from __future__ import annotations

import json
import ctypes
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import worker_supervisor  # noqa: E402
from aiworkhub.worker_workspace import write_json_0600  # noqa: E402


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


def _run_supervisor(spec_path: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, str(Path(worker_supervisor.__file__)), "--spec", str(spec_path)],
        cwd="/",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        shell=False,
    )


def _read_status(path: Path) -> dict:
    deadline = time.monotonic() + 1.0
    while True:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


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


def _assert_timeout_not_enforced(status: dict, timeout_seconds: int) -> None:
    assert status["deadline_epoch"] is None
    assert status["timeout_enforced"] is False
    assert status["timeout_seconds"] == timeout_seconds


def _install_fake_clock(
    monkeypatch: pytest.MonkeyPatch, clock: _FakeClock
) -> None:
    monkeypatch.setattr(worker_supervisor.time, "time", clock.time)
    monkeypatch.setattr(worker_supervisor.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(worker_supervisor.time, "sleep", clock.sleep)


def test_supervisor_success_persists_status_and_private_logs(tmp_path: Path) -> None:
    spec_path, spec = _spec(
        tmp_path,
        [sys.executable, "-c", "print('worker-ok')"],
    )
    result = _run_supervisor(spec_path)
    assert result.returncode == 0, result.stderr.decode()
    assert not spec_path.exists()
    status = _read_status(Path(spec["status_path"]))
    assert status["state"] == "exited"
    assert status["exit_code"] == 0
    _assert_timeout_not_enforced(status, 10)
    assert status["token_budget"]["telemetry_authority"] == "telemetry_unavailable"
    assert status["token_budget"]["telemetry_observed"] is False
    assert status["token_budget"]["telemetry_reason"] == "no_provider_usage_report_observed"
    assert status["last_meaningful_progress_epoch"] >= status["started_at_epoch"]
    assert status["last_meaningful_phase"] == "provider_output"
    assert Path(spec["stdout_path"]).read_text(encoding="utf-8").strip() == "worker-ok"
    for key in ("status_path", "stdout_path", "stderr_path"):
        assert os.name == "nt" or stat.S_IMODE(Path(spec[key]).stat().st_mode) == 0o600


def test_supervisor_spawn_failure_is_never_reported_as_success(tmp_path: Path) -> None:
    spec_path, spec = _spec(tmp_path, [str(tmp_path / "does-not-exist")])
    result = _run_supervisor(spec_path)
    assert result.returncode == 126
    status = _read_status(Path(spec["status_path"]))
    assert status["state"] == "spawn_failed"
    assert status["exit_code"] == 126
    _assert_timeout_not_enforced(status, 10)
    assert "FileNotFoundError" in status["error"]


def test_latest_progress_event_is_bounded_and_uses_newest_sequence(tmp_path: Path) -> None:
    output = tmp_path / "stdout.log"
    output.write_text(
        "not-json\n"
        + json.dumps({"type": "aiworkhub_progress", "sequence": 1, "phase": "request_accepted"})
        + "\n"
        + json.dumps({"type": "aiworkhub_progress", "sequence": 2, "phase": "tool_turn"})
        + "\n",
        encoding="utf-8",
    )

    assert worker_supervisor._latest_progress_event(output) == {
        "sequence": 2,
        "phase": "tool_turn",
    }


def test_latest_progress_event_preserves_bounded_tool_timeout_diagnostic(
    tmp_path: Path,
) -> None:
    output = tmp_path / "stdout.log"
    output.write_text(json.dumps({
        "type": "aiworkhub_progress",
        "sequence": 3,
        "phase": "tool_turn",
        "tool_name": "aiworkhub_worker_quality_review_submit",
        "tool_state": "failed",
        "elapsed_ms": 120001,
        "error_code": "mcp_request_timeout",
        "timeout_phase": "request_wait",
        "timeout_ms": 120000,
    }) + "\n", encoding="utf-8")

    assert worker_supervisor._latest_progress_event(output) == {
        "sequence": 3,
        "phase": "tool_turn",
        "tool_name": "aiworkhub_worker_quality_review_submit",
        "tool_state": "failed",
        "elapsed_ms": 120001,
        "error_code": "mcp_request_timeout",
        "timeout_phase": "request_wait",
        "timeout_ms": 120000,
    }


def test_progress_tail_preserves_meaningful_event_before_newer_liveness(tmp_path: Path) -> None:
    output = tmp_path / "stdout.log"
    events = [
        {"type": "aiworkhub_progress", "sequence": 1, "phase": "request_accepted"},
        {"type": "aiworkhub_progress", "sequence": 2, "phase": "tool_turn"},
        {"type": "aiworkhub_progress", "sequence": 3, "phase": "provider_response"},
    ]
    output.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )

    latest, meaningful = worker_supervisor._latest_progress_events(output)

    assert latest == {"sequence": 3, "phase": "provider_response"}
    assert meaningful == {"sequence": 2, "phase": "tool_turn"}


def test_short_trusted_worker_persists_preterminal_meaningful_event(
    tmp_path: Path,
) -> None:
    events = [
        {"type": "aiworkhub_progress", "sequence": 1, "phase": "request_accepted"},
        {"type": "aiworkhub_progress", "sequence": 2, "phase": "tool_turn"},
        {"type": "aiworkhub_progress", "sequence": 3, "phase": "provider_response"},
    ]
    script = (
        "import json; events=" + repr(events) + "; "
        "[print(json.dumps(event), flush=True) for event in events]"
    )
    spec_path, spec = _spec(tmp_path, [sys.executable, "-c", script])
    spec.update(adapter_id="vscode_lm", heartbeat_interval_seconds=60)
    write_json_0600(spec_path, spec)

    result = _run_supervisor(spec_path)

    assert result.returncode == 0, result.stderr.decode()
    status = _read_status(Path(spec["status_path"]))
    assert status["last_progress_sequence"] == 3
    assert status["last_meaningful_progress_sequence"] == 2
    assert status["last_meaningful_phase"] == "tool_turn"


def test_supervisor_persists_trusted_progress_phase(tmp_path: Path) -> None:
    event = {"type": "aiworkhub_progress", "sequence": 3, "phase": "final_edit"}
    script = (
        "import json,time; "
        f"print(json.dumps({event!r}), flush=True); "
        "time.sleep(.2)"
    )
    spec_path, spec = _spec(tmp_path, [sys.executable, "-c", script])
    spec.update(adapter_id="vscode_lm", heartbeat_interval_seconds=0.05)
    write_json_0600(spec_path, spec)

    result = _run_supervisor(spec_path)

    assert result.returncode == 0, result.stderr.decode()
    status = _read_status(Path(spec["status_path"]))
    assert status["last_progress_sequence"] == 3
    assert status["last_meaningful_progress_sequence"] == 3
    assert status["last_meaningful_phase"] == "final_edit"


@pytest.mark.parametrize("adapter_id", sorted(worker_supervisor.TRUSTED_PROGRESS_ADAPTERS))
def test_provider_response_progress_is_liveness_only(
    tmp_path: Path, adapter_id: str,
) -> None:
    events = [
        {"type": "aiworkhub_progress", "sequence": 1, "phase": "request_accepted"},
        {"type": "aiworkhub_progress", "sequence": 2, "phase": "tool_turn"},
        {"type": "aiworkhub_progress", "sequence": 3, "phase": "provider_response"},
        {"type": "aiworkhub_progress", "sequence": 4, "phase": "provider_response"},
    ]
    script = (
        "import json,time; events=" + repr(events) + "; "
        "[(print(json.dumps(event), flush=True), time.sleep(.08)) for event in events]"
    )
    spec_path, spec = _spec(tmp_path, [sys.executable, "-c", script])
    spec.update(adapter_id=adapter_id, heartbeat_interval_seconds=0.03)
    write_json_0600(spec_path, spec)

    result = _run_supervisor(spec_path)

    assert result.returncode == 0, result.stderr.decode()
    status = _read_status(Path(spec["status_path"]))
    assert status["last_progress_sequence"] == 4
    assert status["last_meaningful_progress_sequence"] == 2
    assert status["last_meaningful_phase"] == "tool_turn"
    assert status["last_output_change_epoch"] >= status["last_meaningful_progress_epoch"]


def test_supervisor_error_status_salvages_bounded_child_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NF-2026-00082: when the primary lifecycle write faults after the child
    exits, the supervisor must fail closed and persist a supervisor_error
    status carrying the bounded child stdout_tail, stderr_tail and
    child_returncode so the diagnostic cannot be lost inside the validation
    sandbox.

    supervise() is called directly so the validation sandbox does not need
    supervisor->child nested process depth. status writes are monkeypatched
    by call/state: writes for the starting state and running heartbeats are
    allowed; the final exited write is forced to raise so the outer
    supervisor_error salvage branch runs deterministically. No directory
    chmod tricks, no missing-directory assumptions and no skips are used so
    execution stays portable under Landlock, seccomp and process isolation."""
    _spec_path, spec = _spec(
        tmp_path,
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('salvage-out\\n'); sys.stdout.flush();"
            " sys.stderr.write('salvage-err\\n'); sys.stderr.flush();"
            " sys.exit(42)",
        ],
    )

    real_write = worker_supervisor._write_json_0600
    recorded: list[dict] = []

    def selective_write(path, payload):
        snapshot = dict(payload)
        recorded.append(snapshot)
        if snapshot.get("state") == "exited":
            raise OSError("simulated_exited_status_write_failure")
        real_write(path, payload)

    # supervise() calls signal.signal(SIGTERM/SIGINT); calling it in-process
    # must not leak those handlers into pytest. The production supervisor
    # still installs them when it runs as its own dedicated process.
    monkeypatch.setattr(worker_supervisor.signal, "signal", lambda *a, **k: None)
    monkeypatch.setattr(worker_supervisor, "_write_json_0600", selective_write)

    rc = worker_supervisor.supervise(spec)

    assert rc != 0
    salvage = next(p for p in recorded if p.get("state") == "supervisor_error")
    assert salvage["state"] == "supervisor_error"
    assert "salvage-out" in salvage["stdout_tail"]
    assert "salvage-err" in salvage["stderr_tail"]
    assert salvage["child_returncode"] == 42


def test_supervisor_status_write_failure_emits_bounded_stderr_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """NF-2026-00082: when both the primary lifecycle write and the
    supervisor_error salvage artifact cannot be persisted, the supervisor
    must fail closed and emit a bounded structured stderr fallback carrying
    the bounded child stdout_tail/stderr_tail and child_returncode so the
    diagnostic is not silently lost inside the validation sandbox.

    supervise() is called directly so the validation sandbox does not need
    supervisor->child nested process depth. status writes are monkeypatched
    by call/state: writes for the starting state and running heartbeats are
    allowed; the final exited write and the supervisor_error salvage write
    both raise so the structured stderr fallback runs deterministically. No
    directory chmod tricks, no missing-directory assumptions and no skips are
    used so execution stays portable under Landlock, seccomp and process
    isolation."""
    spec_path, spec = _spec(
        tmp_path,
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('fallback-out\\n'); sys.stdout.flush();"
            " sys.stderr.write('fallback-err\\n'); sys.stderr.flush();"
            " sys.exit(7)",
        ],
    )

    real_write = worker_supervisor._write_json_0600

    def selective_write(path, payload):
        state = payload.get("state")
        if state in ("exited", "supervisor_error"):
            raise OSError(f"simulated_{state}_status_write_failure")
        real_write(path, payload)
    # supervise() calls signal.signal(SIGTERM/SIGINT); calling it in-process
    # must not leak those handlers into pytest. The production supervisor
    # still installs them when it runs as its own dedicated process.
    monkeypatch.setattr(worker_supervisor.signal, "signal", lambda *a, **k: None)
    monkeypatch.setattr(worker_supervisor, "_write_json_0600", selective_write)

    rc = worker_supervisor.supervise(spec)

    assert rc != 0
    fallback = capsys.readouterr().err
    assert "fallback-out" in fallback
    assert "fallback-err" in fallback
    assert "7" in fallback


def test_supervisor_bounds_verbose_output_and_keeps_tail(tmp_path: Path) -> None:
    spec_path, spec = _spec(
        tmp_path,
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('x' * 20000 + 'FINAL-TAIL\\n')",
        ],
    )
    spec["max_output_bytes"] = 2048
    write_json_0600(spec_path, spec)

    result = _run_supervisor(spec_path)

    assert result.returncode == 0, result.stderr.decode()
    output = Path(spec["stdout_path"]).read_bytes()
    status = _read_status(Path(spec["status_path"]))
    assert len(output) <= 2048
    assert b"earlier worker output truncated" in output
    assert b"FINAL-TAIL" in output
    assert status["stdout_dropped_bytes"] > 0


def test_fake_clock_past_legacy_timeout_does_not_kill_live_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _FakeClock()
    _install_fake_clock(monkeypatch, clock)
    timeout_seconds = 2
    _, spec = _spec(
        tmp_path,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        timeout=timeout_seconds,
    )
    spec["heartbeat_interval_seconds"] = 0.05
    started_mono = clock.mono
    payloads: list[dict] = []
    terminate_calls = {"n": 0}
    saw_running_past_timeout = False
    original_write = worker_supervisor._write_json_0600
    original_terminate = worker_supervisor._terminate_child

    def capturing_write(path: Path, payload: dict) -> None:
        payloads.append(dict(payload))
        original_write(path, payload)

    def wrapped_terminate(child):
        terminate_calls["n"] += 1
        return original_terminate(child)

    def fake_sleep(seconds: float) -> None:
        nonlocal saw_running_past_timeout
        clock.sleep(seconds)
        status_path = Path(spec["status_path"])
        if not status_path.is_file() or saw_running_past_timeout:
            return
        status = json.loads(status_path.read_text(encoding="utf-8"))
        if status.get("state") != "running":
            return
        if clock.mono - started_mono <= timeout_seconds:
            return
        _assert_timeout_not_enforced(status, timeout_seconds)
        os.kill(int(status["child_pid"]), 0)
        saw_running_past_timeout = True
        write_json_0600(Path(spec["cancel_path"]), {"reason": "after-fake-timeout"})

    monkeypatch.setattr(worker_supervisor.time, "sleep", fake_sleep)
    monkeypatch.setattr(worker_supervisor, "_write_json_0600", capturing_write)
    monkeypatch.setattr(worker_supervisor, "_terminate_child", wrapped_terminate)

    code = worker_supervisor.supervise(spec)

    assert saw_running_past_timeout
    assert clock.mono - started_mono > timeout_seconds
    assert code == 125
    assert terminate_calls["n"] == 1
    states = [payload["state"] for payload in payloads]
    assert "starting" in states
    assert "running" in states
    assert states[-1] == "cancelled"
    assert "timed_out" not in states
    for payload in payloads:
        _assert_timeout_not_enforced(payload, timeout_seconds)


def test_supervisor_does_not_terminate_when_live_usage_crosses_legacy_cap(
    tmp_path: Path,
) -> None:
    # A deterministic provider stream crosses a legacy cap while running and
    # must continue to ordinary completion: usage never signals or reaps it.
    script = (
        "import json,time; "
        "print(json.dumps({'usage': {'input_tokens': 9, 'output_tokens': 4}}), flush=True); "
        "time.sleep(.4)"
    )
    spec_path, spec = _spec(tmp_path, [sys.executable, "-c", script])
    spec.update(
        adapter_id="vscode_lm",
        token_budget={"cap_tokens": 10},
        heartbeat_interval_seconds=0.05,
    )
    write_json_0600(spec_path, spec)

    result = _run_supervisor(spec_path)

    assert result.returncode == 0, result.stderr.decode()
    status = _read_status(Path(spec["status_path"]))
    assert status["state"] == "exited"
    assert status["state"] != "token_budget_exceeded"
    _assert_timeout_not_enforced(status, 10)
    assert status["error"] == ""
    # Usage is still recorded, explicitly labeled non-enforcing telemetry.
    assert status["token_budget"]["cap_tokens"] == 10
    assert status["token_budget"]["accepted_total_tokens"] == 13
    assert status["token_budget"]["enforcing"] is False
    assert status["token_budget"]["cap_enforceable"] is False
    assert status["token_budget"]["events"][-1]["cap_enforceable"] is False


def test_supervisor_records_claude_turn_usage_without_enforcing_legacy_cap(
    tmp_path: Path,
) -> None:
    event = {
        "type": "stream_event",
        "event": {
            "type": "message_delta",
            "usage": {
                "input_tokens": 2,
                "output_tokens": 4,
                "cache_read_input_tokens": 40,
                "cache_creation_input_tokens": 10,
            },
        },
    }
    script = (
        "import json,time; "
        f"event={event!r}; "
        "print(json.dumps(event), flush=True); time.sleep(.15); "
        "print(json.dumps(event), flush=True); time.sleep(.4)"
    )
    spec_path, spec = _spec(tmp_path, [sys.executable, "-c", script])
    spec.update(
        adapter_id="claude_cli",
        token_budget={"cap_tokens": 100},
        heartbeat_interval_seconds=0.05,
    )
    write_json_0600(spec_path, spec)

    result = _run_supervisor(spec_path)

    assert result.returncode == 0, result.stderr.decode()
    status = _read_status(Path(spec["status_path"]))
    assert status["state"] == "exited"
    assert status["state"] != "token_budget_exceeded"
    _assert_timeout_not_enforced(status, 10)
    assert status["token_budget"]["accepted_total_tokens"] == 112
    assert status["token_budget"]["enforcing"] is False
    assert status["token_budget"]["events"][-1]["cap_enforceable"] is False


def test_supervisor_does_not_terminate_on_output_bytes(
    tmp_path: Path,
) -> None:
    script = "import sys; sys.stdout.write('x' * 4096); sys.stdout.flush()"
    spec_path, spec = _spec(tmp_path, [sys.executable, "-c", script])
    spec.update(
        max_total_output_bytes=2048,
        heartbeat_interval_seconds=0.05,
        timeout_seconds=1,
    )
    write_json_0600(spec_path, spec)

    result = _run_supervisor(spec_path)

    assert result.returncode == 0, result.stderr.decode()
    status = _read_status(Path(spec["status_path"]))
    assert status["state"] == "exited"
    assert status["state"] != "output_budget_exceeded"
    _assert_timeout_not_enforced(status, 1)
    assert status["output_budget"]["cap_bytes"] == 2048
    assert status["output_budget"]["observed_bytes"] >= 4096
    assert status["output_budget"]["byte_labels_are_token_truth"] is False
    assert status["token_budget"]["telemetry_observed"] is False


def test_terminal_only_usage_is_posthoc_and_never_claimed_enforced(tmp_path: Path) -> None:
    script = "import json; print(json.dumps({'usage': {'input_tokens': 9, 'output_tokens': 4}}))"
    spec_path, spec = _spec(tmp_path, [sys.executable, "-c", script])
    spec.update(
        adapter_id="vscode_lm",
        token_budget={"cap_tokens": 10},
        heartbeat_interval_seconds=30,
    )
    write_json_0600(spec_path, spec)

    result = _run_supervisor(spec_path)

    assert result.returncode == 0, result.stderr.decode()
    status = _read_status(Path(spec["status_path"]))
    assert status["state"] == "exited"
    _assert_timeout_not_enforced(status, 10)
    assert status["token_budget"]["accepted_total_tokens"] == 13
    assert status["token_budget"]["enforceable_live_tokens"] == 0
    assert status["token_budget"]["enforcing"] is False
    assert status["token_budget"]["events"][-1]["cap_enforceable"] is False


def test_cancel_marker_and_signal_survive_manager_restart_boundary(tmp_path: Path) -> None:
    spec_path, spec = _spec(
        tmp_path,
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    process = subprocess.Popen(
        [sys.executable, str(Path(worker_supervisor.__file__)), "--spec", str(spec_path)],
        cwd="/",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        # Validation sandboxes may deny the outer pytest->supervisor setsid;
        # production child isolation (worker start_new_session + Linux PDEATHSIG
        # created by the supervisor itself) is asserted separately by
        # test_posix_worker_spawn_kwargs_are_platform_specific.
    )
    status_path = Path(spec["status_path"])
    deadline = time.monotonic() + 5
    status: dict = {}
    while time.monotonic() < deadline:
        if status_path.is_file():
            status = _read_status(status_path)
            if status.get("state") == "running":
                break
        time.sleep(0.02)
    assert status.get("state") == "running"
    _assert_timeout_not_enforced(status, 10)
    child_pid = int(status["child_pid"])

    cancel_path = Path(spec["cancel_path"])
    write_json_0600(cancel_path, {"reason": "test-restart-cancel"})
    if os.name != "nt":
        os.kill(process.pid, signal.SIGTERM)
    assert process.wait(timeout=5) == 125
    final = _read_status(status_path)
    assert final["state"] == "cancelled"
    _assert_timeout_not_enforced(final, 10)
    assert final["child_pid"] == child_pid
    assert os.name == "nt" or stat.S_IMODE(status_path.stat().st_mode) == 0o600
    assert not cancel_path.exists()
    missing_process_error = OSError if os.name == "nt" else ProcessLookupError
    with pytest.raises(missing_process_error):
        os.kill(child_pid, 0)


def test_abrupt_supervisor_loss_does_not_orphan_worker(tmp_path: Path) -> None:
    spec_path, spec = _spec(
        tmp_path,
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    process = subprocess.Popen(
        [sys.executable, str(Path(worker_supervisor.__file__)), "--spec", str(spec_path)],
        cwd="/",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        # Validation sandboxes may deny the outer pytest->supervisor setsid;
        # production child isolation (worker start_new_session + Linux PDEATHSIG
        # created by the supervisor itself) is asserted separately by
        # test_posix_worker_spawn_kwargs_are_platform_specific.
    )
    status_path = Path(spec["status_path"])
    deadline = time.monotonic() + 5
    status: dict = {}
    while time.monotonic() < deadline:
        if status_path.is_file():
            status = _read_status(status_path)
            if status.get("state") == "running":
                break
        time.sleep(0.02)
    assert status.get("state") == "running"
    child_pid = int(status["child_pid"])

    if os.name == "nt":
        process.kill()
        assert process.wait(timeout=5) != 0
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            handle = kernel32.OpenProcess(0x1000, False, child_pid)
            if not handle:
                break
            kernel32.CloseHandle(handle)
            time.sleep(0.02)
        else:
            raise AssertionError("worker survived abrupt supervisor loss")
        return

    os.kill(process.pid, signal.SIGKILL)
    assert process.wait(timeout=5) == -signal.SIGKILL
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        proc_stat = Path(f"/proc/{child_pid}/stat")
        if not proc_stat.exists():
            break
        try:
            raw = proc_stat.read_text(encoding="utf-8")
        except (FileNotFoundError, ProcessLookupError):
            # The worker may exit between the existence probe and the read;
            # disappearing from procfs is the successful outcome under test.
            break
        if raw.rpartition(")")[2].strip().split()[0] == "Z":
            break
        time.sleep(0.02)
    else:
        raise AssertionError("worker survived abrupt supervisor loss")


def test_supervisor_never_enables_shell_execution() -> None:
    source = Path(worker_supervisor.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "os.system(" not in source


def test_appcontainer_terminate_then_wait_does_not_bypass_native_result(
    tmp_path: Path,
) -> None:
    class FakeLaunch:
        pid = 41
        command_line = "worker.exe"

        def __init__(self) -> None:
            self.wait_calls = 0
            self.close_calls = 0

        def terminate(self, exit_code: int):
            assert exit_code == 1
            return worker_supervisor.windows_appcontainer.AppContainerLifecycleResult(
                worker_supervisor.windows_appcontainer.AppContainerLifecycleState.EXITED,
                exit_code=73,
            )

        def wait(self, timeout_ms: int):
            self.wait_calls += 1
            assert timeout_ms == 1000
            return worker_supervisor.windows_appcontainer.AppContainerLifecycleResult(
                worker_supervisor.windows_appcontainer.AppContainerLifecycleState.EXITED,
                exit_code=73,
            )

        def close(self) -> None:
            self.close_calls += 1

    launch = FakeLaunch()
    stdout = (tmp_path / "stdout").open("w+b")
    stderr = (tmp_path / "stderr").open("w+b")
    process = worker_supervisor._AppContainerProcess(launch, stdout, stderr)

    process.terminate()
    assert process.returncode is None
    assert process.wait(timeout=1) == 73
    assert process.returncode == 73
    assert launch.wait_calls == 1
    assert launch.close_calls == 1
    process.close()
    assert launch.close_calls == 1


def test_posix_worker_spawn_kwargs_are_platform_specific() -> None:
    linux = worker_supervisor._posix_worker_spawn_kwargs("linux")
    macos = worker_supervisor._posix_worker_spawn_kwargs("darwin")

    assert linux["start_new_session"] is True
    assert linux["preexec_fn"] is worker_supervisor._die_with_supervisor
    assert macos == {"start_new_session": True}


def test_usage_total_from_output_fails_soft_on_deep_recursion(tmp_path: Path) -> None:
    output = tmp_path / "stdout.log"
    output.write_text("[" * 20000, encoding="utf-8")

    assert worker_supervisor._usage_total_from_output(output, "codex_cli") is None


def test_usage_total_from_output_fails_soft_on_oversized_line(tmp_path: Path) -> None:
    output = tmp_path / "stdout.log"
    output.write_text(
        json.dumps({"usage": {"input_tokens": 1}})
        + ("x" * worker_supervisor.MAX_USAGE_SCAN_BYTES),
        encoding="utf-8",
    )

    assert worker_supervisor._usage_total_from_output(output, "codex_cli") is None


def test_usage_total_from_output_still_recognizes_normal_usage(tmp_path: Path) -> None:
    output = tmp_path / "stdout.log"
    output.write_text(
        json.dumps({"usage": {"input_tokens": 9, "output_tokens": 4}}) + "\n",
        encoding="utf-8",
    )

    assert worker_supervisor._usage_total_from_output(output, "codex_cli") == 13


def test_latest_progress_events_fail_soft_on_deep_recursion(tmp_path: Path) -> None:
    output = tmp_path / "stdout.log"
    output.write_text(
        json.dumps({"type": "aiworkhub_progress", "sequence": 1, "phase": "tool_turn"})
        + "\n"
        + "[" * 20000
        + "\n",
        encoding="utf-8",
    )

    assert worker_supervisor._latest_progress_events(output) == (
        {"sequence": 1, "phase": "tool_turn"},
        {"sequence": 1, "phase": "tool_turn"},
    )


def test_supervisor_survives_deeply_nested_stdout_line(tmp_path: Path) -> None:
    script = "print('[' * 20000)"
    spec_path, spec = _spec(tmp_path, [sys.executable, "-c", script])

    result = _run_supervisor(spec_path)

    assert result.returncode == 0, result.stderr.decode()
    status = _read_status(Path(spec["status_path"]))
    assert status["state"] == "exited"
    assert status["exit_code"] == 0
    _assert_timeout_not_enforced(status, 10)


def test_fake_clock_explicit_cancel_is_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _FakeClock()
    _install_fake_clock(monkeypatch, clock)
    _, spec = _spec(
        tmp_path,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        timeout=30,
    )
    spec["heartbeat_interval_seconds"] = 0.05
    terminate_calls = {"n": 0}
    original_terminate = worker_supervisor._terminate_child

    def wrapped_terminate(child):
        terminate_calls["n"] += 1
        return original_terminate(child)

    def fake_sleep(seconds: float) -> None:
        clock.sleep(seconds)
        cancel_path = Path(spec["cancel_path"])
        if not cancel_path.exists():
            write_json_0600(cancel_path, {"reason": "explicit-cancel"})

    monkeypatch.setattr(worker_supervisor.time, "sleep", fake_sleep)
    monkeypatch.setattr(worker_supervisor, "_terminate_child", wrapped_terminate)

    code = worker_supervisor.supervise(spec)

    assert code == 125
    assert terminate_calls["n"] == 1
    status = json.loads(Path(spec["status_path"]).read_text(encoding="utf-8"))
    assert status["state"] == "cancelled"
    _assert_timeout_not_enforced(status, 30)


def test_fake_clock_exact_child_exit_is_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _FakeClock()
    _install_fake_clock(monkeypatch, clock)
    timeout_seconds = 2
    marker = tmp_path / "exit.marker"
    script = (
        "import pathlib\n"
        f"marker=pathlib.Path({str(marker)!r})\n"
        "while not marker.exists():\n"
        "    pass\n"
    )
    _, spec = _spec(tmp_path, [sys.executable, "-c", script], timeout=timeout_seconds)
    spec["heartbeat_interval_seconds"] = 0.05
    started_mono = clock.mono
    terminate_calls = {"n": 0}
    original_terminate = worker_supervisor._terminate_child

    def wrapped_terminate(child):
        terminate_calls["n"] += 1
        return original_terminate(child)

    def fake_sleep(seconds: float) -> None:
        clock.sleep(seconds)
        if clock.mono - started_mono > timeout_seconds and not marker.exists():
            marker.write_text("exit", encoding="utf-8")

    monkeypatch.setattr(worker_supervisor.time, "sleep", fake_sleep)
    monkeypatch.setattr(worker_supervisor, "_terminate_child", wrapped_terminate)

    code = worker_supervisor.supervise(spec)

    assert clock.mono - started_mono > timeout_seconds
    assert code == 0
    assert terminate_calls["n"] == 0
    status = json.loads(Path(spec["status_path"]).read_text(encoding="utf-8"))
    assert status["state"] == "exited"
    assert status["exit_code"] == 0
    _assert_timeout_not_enforced(status, timeout_seconds)


def test_fake_clock_live_usage_crossing_legacy_cap_never_terminates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _FakeClock()
    _install_fake_clock(monkeypatch, clock)
    timeout_seconds = 2
    script = (
        "import json; "
        "print(json.dumps({'usage': {'input_tokens': 9, 'output_tokens': 4}}), flush=True)"
    )
    _, spec = _spec(tmp_path, [sys.executable, "-c", script], timeout=timeout_seconds)
    spec.update(
        adapter_id="vscode_lm",
        token_budget={"cap_tokens": 10},
        heartbeat_interval_seconds=0.05,
    )
    terminate_calls = {"n": 0}
    original_terminate = worker_supervisor._terminate_child

    def wrapped_terminate(child):
        terminate_calls["n"] += 1
        return original_terminate(child)

    monkeypatch.setattr(worker_supervisor, "_terminate_child", wrapped_terminate)

    code = worker_supervisor.supervise(spec)

    assert code == 0
    assert terminate_calls["n"] == 0
    status = json.loads(Path(spec["status_path"]).read_text(encoding="utf-8"))
    assert status["state"] == "exited"
    assert status["state"] != "token_budget_exceeded"
    _assert_timeout_not_enforced(status, timeout_seconds)
    assert status["token_budget"]["cap_tokens"] == 10
    assert status["token_budget"]["accepted_total_tokens"] == 13
    assert status["token_budget"]["enforcing"] is False
    assert status["token_budget"]["events"][-1]["cap_enforceable"] is False
