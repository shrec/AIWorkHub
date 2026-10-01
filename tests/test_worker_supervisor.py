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
from aiworkhub import platform_io  # noqa: E402
from aiworkhub.worker_workspace import write_json_0600  # noqa: E402


def test_default_live_stream_log_bound_is_four_mib() -> None:
    assert worker_supervisor.DEFAULT_MAX_OUTPUT_BYTES == 4 * 1024 * 1024


def test_supervisor_uses_canonical_platform_chmod_fd_in_package_and_direct_context():
    assert worker_supervisor.chmod_fd is platform_io.chmod_fd

    script = """
import importlib.util
import json
import sys
from pathlib import Path

path = Path('worker_supervisor.py')
spec = importlib.util.spec_from_file_location('direct_worker_supervisor', path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
facade = sys.modules['platform_io']
print(json.dumps({
    'facade': facade.__name__,
    'chmod_fd_identity': module.chmod_fd is facade.chmod_fd,
}))
"""
    package_root = Path(worker_supervisor.__file__).resolve().parent
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=package_root,
        env={**os.environ, "PYTHONPATH": str(package_root)},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    assert json.loads(result.stdout) == {
        "facade": "platform_io",
        "chmod_fd_identity": True,
    }


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


def _assert_hard_deadline(status: dict, timeout_seconds: int) -> None:
    assert status["deadline_epoch"] == status["started_at_epoch"] + timeout_seconds
    assert status["timeout_enforced"] is True
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
    _assert_hard_deadline(status, 10)
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
    _assert_hard_deadline(status, 10)
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


def test_fake_clock_hard_deadline_kills_live_child(
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
    terminate_calls = {"n": 0, "mono": None}
    original_write = worker_supervisor._write_json_0600
    original_terminate = worker_supervisor._terminate_child

    def capturing_write(path: Path, payload: dict) -> None:
        payloads.append(dict(payload))
        original_write(path, payload)

    def wrapped_terminate(child):
        terminate_calls["n"] += 1
        terminate_calls["mono"] = clock.mono
        return original_terminate(child)

    monkeypatch.setattr(worker_supervisor, "_write_json_0600", capturing_write)
    monkeypatch.setattr(worker_supervisor, "_terminate_child", wrapped_terminate)

    code = worker_supervisor.supervise(spec)

    assert terminate_calls["mono"] is not None
    termination_delta = terminate_calls["mono"] - started_mono
    assert termination_delta >= timeout_seconds
    assert termination_delta < timeout_seconds + worker_supervisor.POLL_SECONDS + 1e-9
    assert code == 124
    assert terminate_calls["n"] == 1
    states = [payload["state"] for payload in payloads]
    assert "starting" in states
    assert "running" in states
    assert states[-1] == "timed_out"
    assert states.count("timed_out") == 1
    assert "cancelled" not in states
    for payload in payloads:
        _assert_hard_deadline(payload, timeout_seconds)


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
    _assert_hard_deadline(status, 10)
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
    _assert_hard_deadline(status, 10)
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
    _assert_hard_deadline(status, 1)
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
    _assert_hard_deadline(status, 10)
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
    _assert_hard_deadline(status, 10)
    child_pid = int(status["child_pid"])

    cancel_path = Path(spec["cancel_path"])
    write_json_0600(cancel_path, {"reason": "test-restart-cancel"})
    if os.name != "nt":
        os.kill(process.pid, signal.SIGTERM)
    assert process.wait(timeout=5) == 125
    final = _read_status(status_path)
    assert final["state"] == "cancelled"
    _assert_hard_deadline(final, 10)
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


# -- worker MCP bridge (NF-2026-00034) ---------------------------------------


class _FakePipe:
    """Stands in for windows_appcontainer.WorkerPipe: scripted client bytes."""

    def __init__(self, *, accept: bool = True) -> None:
        import queue

        self._inbound: queue.Queue[bytes] = queue.Queue()
        self._accept = accept
        self.accepted: list[object] = []
        self.written = bytearray()
        self.shut = False
        self.closed = 0

    def accept(self, job, timeout=None):
        self.accepted.append(job)
        self.accept_timeout = timeout
        while not self._accept and not self.shut:
            time.sleep(0.01)
        return self._accept and not self.shut

    def feed(self, data: bytes) -> None:
        self._inbound.put(data)

    def read(self) -> bytes:
        import queue

        while not self.shut:
            try:
                return self._inbound.get(timeout=0.02)
            except queue.Empty:
                continue
        return b""

    def write(self, data: bytes) -> None:
        if self.shut:
            raise BrokenPipeError("shut")
        self.written += data

    def shutdown(self) -> None:
        self.shut = True

    def close(self, timeout: float = 5.0) -> bool:
        self.shut = True
        self.closed += 1
        return True


class _FakeServerJob:
    def __init__(self) -> None:
        self.assigned: list[object] = []
        self.closed = False

    def assign(self, process) -> None:
        self.assigned.append(process)

    def close(self) -> None:
        self.closed = True


# Echoes its bound environment once, then every stdin line: an MCP server's
# stdio shape without its content.
_ECHO_SERVER = (
    "import os,sys\n"
    "sys.stdout.buffer.write(os.environ['BRIDGE_PROBE'].encode()+b'\\n')\n"
    "sys.stdout.buffer.flush()\n"
    "for line in sys.stdin.buffer:\n"
    "    sys.stdout.buffer.write(line)\n"
    "    sys.stdout.buffer.flush()\n"
)


def _bridge(tmp_path: Path, pipe: _FakePipe, jobs: list, *, writable=(), **bridge):
    private = tmp_path / "private"
    private.mkdir(exist_ok=True)
    spec = {
        "command": sys.executable,
        "args": ["-c", _ECHO_SERVER],
        "env": {"BRIDGE_PROBE": "bound-env"},
        "cwd": str(private),
        "stderr_path": str(tmp_path / "worker-mcp.stderr.log"),
        **bridge,
    }

    def _job():
        jobs.append(_FakeServerJob())
        return jobs[-1]

    return worker_supervisor._WorkerMcpBridge(pipe, spec, list(writable), server_job=_job)


def _wait_until(predicate, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def test_bridge_serves_one_client_of_the_job_and_tears_everything_down(
    tmp_path: Path,
) -> None:
    pipe, jobs = _FakePipe(), []
    bridge = _bridge(tmp_path, pipe, jobs)
    bridge.start("launch-job")
    pipe.feed(b'{"jsonrpc":"2.0","id":1,"method":"initialize"}\n')
    _wait_until(lambda: b'"id":1' in pipe.written)

    # One accept, against this launch's job; the server started with the
    # config's own environment and was put in its own kill-on-close job.
    assert pipe.accepted == ["launch-job"]
    assert bytes(pipe.written).startswith(b"bound-env\n")
    server = bridge.server
    assert jobs[0].assigned == [server]

    bridge.close()
    assert server.poll() is not None  # no orphan server
    assert jobs[0].closed
    assert pipe.shut and pipe.closed >= 1  # no leftover pipe
    assert bridge.error == ""
    bridge.close()  # idempotent


def test_bridge_client_eof_lets_the_server_exit_and_closes_the_pipe(tmp_path: Path) -> None:
    pipe, jobs = _FakePipe(), []
    bridge = _bridge(tmp_path, pipe, jobs)
    bridge.start("launch-job")
    _wait_until(lambda: bridge.server is not None)
    pipe.feed(b"")  # the contained client hung up
    _wait_until(lambda: bridge.server.poll() is not None and pipe.closed >= 1)
    bridge.close()
    assert bridge.error == ""


def test_bridge_that_is_never_connected_starts_no_server(tmp_path: Path) -> None:
    pipe, jobs = _FakePipe(accept=False), []
    bridge = _bridge(tmp_path, pipe, jobs)
    bridge.start("launch-job")
    time.sleep(0.1)
    bridge.close()
    assert bridge.server is None and jobs == []
    assert pipe.closed >= 1


def test_a_bridge_closed_before_it_starts_removes_its_pipe(tmp_path: Path) -> None:
    pipe = _FakePipe()
    _bridge(tmp_path, pipe, []).close()
    assert pipe.closed == 1 and pipe.accepted == []


def test_bridge_server_that_cannot_start_leaves_nothing_behind(tmp_path: Path) -> None:
    pipe, jobs = _FakePipe(), []
    bridge = _bridge(tmp_path, pipe, jobs, command=str(tmp_path / "missing.exe"))
    bridge.start("launch-job")
    # The waiting client is let go at once instead of at worker exit.
    _wait_until(lambda: bridge.error != "" and pipe.closed >= 1)
    bridge.close()
    assert bridge.server is None and jobs == []
    assert "FileNotFoundError" in bridge.error or "OSError" in bridge.error


_PLANT_PROBE = (
    "import sys\n"
    "try:\n"
    "    import aiworkhub_planted\n"
    "    print('PLANTED', flush=True)\n"
    "except ImportError:\n"
    "    print('clean', flush=True)\n"
    "print(repr(sys.path), flush=True)\n"
)


def test_the_host_server_never_imports_what_the_container_can_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The review's attack: the model writes a module into its worktree,
    then connects -- the host server must not import it, whether through
    the working directory, PYTHONPATH, PYTHONSTARTUP or the user site."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "aiworkhub_planted.py").write_text("print('ran')\n", encoding="utf-8")
    (worktree / "startup.py").write_text("import aiworkhub_planted\n", encoding="utf-8")
    monkeypatch.chdir(worktree)  # the supervisor's own cwd is the worktree
    for key, value in {
        "PYTHONPATH": str(worktree), "PYTHONSTARTUP": str(worktree / "startup.py"),
        "PYTHONHOME": str(worktree), "HOME": str(worktree), "USERPROFILE": str(worktree),
        "APPDATA": str(worktree), "TMP": str(worktree), "TEMP": str(worktree),
    }.items():
        monkeypatch.setenv(key, value)
    pipe, jobs = _FakePipe(), []
    bridge = _bridge(
        tmp_path, pipe, jobs, writable=[str(worktree)], args=["-c", _PLANT_PROBE], env={}
    )
    private = str(tmp_path / "private")

    # The environment is rebuilt, not inherited.
    assert bridge._argv[1:3] == ["-P", "-s"]
    assert {k: bridge._env[k] for k in ("PYTHONSAFEPATH", "PYTHONNOUSERSITE")} == {
        "PYTHONSAFEPATH": "1", "PYTHONNOUSERSITE": "1",
    }
    assert not {"PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME", "APPDATA"} & set(bridge._env)
    assert all(bridge._env[k] == private for k in ("HOME", "USERPROFILE", "TMP", "TEMP"))
    # The real import path has nothing the container can write ...
    assert not any(
        str(entry).lower().startswith(str(worktree).lower()) for entry in bridge.host_sys_path()
    )
    # ... and the server itself, started for real, does not import the plant.
    bridge.start("launch-job")
    _wait_until(lambda: pipe.written.count(b"\n") >= 2)
    bridge.close()
    assert pipe.written.startswith(b"clean")
    assert str(worktree).encode() not in bytes(pipe.written)


@pytest.mark.parametrize("field", ["command", "cwd", "pythonpath"])
def test_a_bridge_input_the_container_can_write_is_refused(tmp_path: Path, field: str) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    planted = str(worktree / "x")
    overrides = {
        "command": {"command": planted},
        "cwd": {"cwd": str(worktree)},
        "pythonpath": {"env": {"PYTHONPATH": planted}},
    }[field]
    with pytest.raises(ValueError, match="container_writable"):
        _bridge(tmp_path, _FakePipe(), [], writable=[str(worktree)], **overrides)


def test_the_withheld_directory_is_private_although_inside_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    runtime = home / "task_mcp_worker_runtime"
    runtime.mkdir(parents=True)
    bridge = _bridge(
        tmp_path, _FakePipe(), [], writable=[str(home)],
        cwd=str(runtime), withheld_directories=[str(runtime)],
    )
    assert bridge._env["TMP"] == str(runtime)


def test_a_server_whose_import_path_reaches_the_container_never_starts(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    def _run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 0, json.dumps([str(worktree), "C:\\ok"]), "")

    pipe, jobs = _FakePipe(), []
    private = tmp_path / "private"
    private.mkdir()
    bridge = worker_supervisor._WorkerMcpBridge(
        pipe,
        {"command": sys.executable, "args": [], "env": {}, "cwd": str(private),
         "stderr_path": str(tmp_path / "e.log")},
        [str(worktree)],
        server_job=lambda: jobs.append(_FakeServerJob()) or jobs[-1],
        run=_run,
    )
    bridge.start("launch-job")
    _wait_until(lambda: bridge.error != "")
    bridge.close()
    assert "sys_path_container_writable" in bridge.error
    assert bridge.server is None and jobs == []
    assert pipe.closed >= 1


def test_appcontainer_process_close_ends_the_bridge_before_the_launch(tmp_path: Path) -> None:
    order: list[str] = []

    class FakeLaunch:
        pid = 41
        command_line = "worker.exe"

        def close(self) -> None:
            order.append("launch")

    class FakeBridge:
        def close(self) -> None:
            order.append("bridge")

    stdout = (tmp_path / "stdout").open("w+b")
    stderr = (tmp_path / "stderr").open("w+b")
    process = worker_supervisor._AppContainerProcess(
        FakeLaunch(), stdout, stderr, (), FakeBridge()
    )
    process.close()
    process.close()
    assert order == ["bridge", "launch"]


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
    _assert_hard_deadline(status, 10)


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
    _assert_hard_deadline(status, 30)


def test_fake_clock_exact_child_exit_is_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_sleep = worker_supervisor.time.sleep
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
    terminate_calls = {"n": 0}
    original_terminate = worker_supervisor._terminate_child

    def wrapped_terminate(child):
        terminate_calls["n"] += 1
        return original_terminate(child)

    def fake_sleep(seconds: float) -> None:
        if not marker.exists():
            marker.write_text("exit", encoding="utf-8")
        # This test owns the successful-exit branch, not deadline expiry. Yield
        # to the real child without advancing the fake deadline; the dedicated
        # hard-deadline test advances the clock and asserts termination.
        real_sleep(min(float(seconds), 0.01))

    monkeypatch.setattr(worker_supervisor.time, "sleep", fake_sleep)
    monkeypatch.setattr(worker_supervisor, "_terminate_child", wrapped_terminate)

    code = worker_supervisor.supervise(spec)

    assert code == 0
    assert terminate_calls["n"] == 0
    status = json.loads(Path(spec["status_path"]).read_text(encoding="utf-8"))
    assert status["state"] == "exited"
    assert status["exit_code"] == 0
    _assert_hard_deadline(status, timeout_seconds)


def test_fake_clock_live_usage_crossing_legacy_cap_never_terminates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_sleep = worker_supervisor.time.sleep
    clock = _FakeClock()
    _install_fake_clock(monkeypatch, clock)

    def scheduled_sleep(seconds: float) -> None:
        clock.sleep(seconds)
        real_sleep(seconds)

    monkeypatch.setattr(worker_supervisor.time, "sleep", scheduled_sleep)
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
    _assert_hard_deadline(status, timeout_seconds)
    assert status["token_budget"]["cap_tokens"] == 10
    assert status["token_budget"]["accepted_total_tokens"] == 13
    assert status["token_budget"]["enforcing"] is False
    assert status["token_budget"]["events"][-1]["cap_enforceable"] is False


def test_fake_clock_output_heartbeat_and_usage_cannot_extend_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _FakeClock()
    _install_fake_clock(monkeypatch, clock)
    timeout_seconds = 2
    script = (
        "import json,sys,time; "
        "print(json.dumps({'usage': {'input_tokens': 9, 'output_tokens': 4}}), flush=True); "
        "print('heartbeat-progress', flush=True); "
        "time.sleep(30)"
    )
    _, spec = _spec(tmp_path, [sys.executable, "-c", script], timeout=timeout_seconds)
    spec.update(
        adapter_id="vscode_lm",
        token_budget={"cap_tokens": 10},
        heartbeat_interval_seconds=0.05,
    )
    started_mono = clock.mono
    terminate_calls = {"n": 0, "mono": None}
    original_terminate = worker_supervisor._terminate_child

    def wrapped_terminate(child):
        terminate_calls["n"] += 1
        terminate_calls["mono"] = clock.mono
        return original_terminate(child)

    monkeypatch.setattr(worker_supervisor, "_terminate_child", wrapped_terminate)

    code = worker_supervisor.supervise(spec)

    assert code == 124
    assert terminate_calls["n"] == 1
    assert terminate_calls["mono"] is not None
    termination_delta = terminate_calls["mono"] - started_mono
    assert termination_delta >= timeout_seconds
    assert termination_delta < timeout_seconds + worker_supervisor.POLL_SECONDS + 1e-9
    status = json.loads(Path(spec["status_path"]).read_text(encoding="utf-8"))
    assert status["state"] == "timed_out"
    _assert_hard_deadline(status, timeout_seconds)
    assert status["token_budget"]["enforcing"] is False


def test_fake_clock_simulated_appcontainer_hard_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _FakeClock()
    _install_fake_clock(monkeypatch, clock)
    timeout_seconds = 2
    _, spec = _spec(tmp_path, [sys.executable, "-c", "pass"], timeout=timeout_seconds)
    spec["execution_backend"] = "windows_appcontainer"
    spec["repo_id"] = "repo-test"
    spec["heartbeat_interval_seconds"] = 0.05
    stdout_r, stdout_w = os.pipe()
    stderr_r, stderr_w = os.pipe()
    os.close(stdout_w)
    os.close(stderr_w)
    lifecycle = worker_supervisor.windows_appcontainer.AppContainerLifecycleState

    class FakeLaunch:
        pid = 41
        command_line = "worker.exe"

        def __init__(self) -> None:
            self.terminated = False

        def terminate(self, exit_code: int):
            self.terminated = True
            return worker_supervisor.windows_appcontainer.AppContainerLifecycleResult(
                lifecycle.EXITED,
                exit_code=1,
            )

        def wait(self, timeout_ms: int):
            if self.terminated:
                return worker_supervisor.windows_appcontainer.AppContainerLifecycleResult(
                    lifecycle.EXITED,
                    exit_code=1,
                )
            return worker_supervisor.windows_appcontainer.AppContainerLifecycleResult(
                lifecycle.RUNNING,
            )

        def close(self) -> None:
            return None

    launch = FakeLaunch()
    process = worker_supervisor._AppContainerProcess(
        launch,
        os.fdopen(stdout_r, "rb", buffering=0),
        os.fdopen(stderr_r, "rb", buffering=0),
    )

    monkeypatch.setattr(
        worker_supervisor,
        "_launch_appcontainer_process",
        lambda argv, cwd, launched_spec, **_kwargs: process,
    )
    started_mono = clock.mono
    code = worker_supervisor.supervise(spec)
    assert code == 124
    assert launch.terminated is True
    assert clock.mono - started_mono >= timeout_seconds
    status = json.loads(Path(spec["status_path"]).read_text(encoding="utf-8"))
    assert status["state"] == "timed_out"
    _assert_hard_deadline(status, timeout_seconds)
    assert status["exit_code"] == 1


def _live_sleeping_child(tmp_path: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=str(tmp_path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
    )


@pytest.mark.parametrize("tree_kill_failure", ["nonzero_exit", "missing_executable"])
def test_terminate_child_ends_live_child_when_tree_kill_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tree_kill_failure: str
) -> None:
    """NF-2026-01166: a tree kill that cannot do its job must not be fatal.

    Inside the Windows AppContainer worker sandbox `taskkill /F /PID <pid> /T`
    exits 1 with "ERROR: The user name or password is incorrect." and the child
    keeps running; a bare-name taskkill that cannot be resolved raises
    FileNotFoundError instead. Either way the supervisor must still end the
    child it spawned, using a handle it owns, and report that child's real exit
    code -- it previously raised TimeoutExpired with the child still alive.
    """
    child = _live_sleeping_child(tmp_path)
    try:
        taskkill_calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            taskkill_calls.append([str(part) for part in argv])
            if tree_kill_failure == "missing_executable":
                raise FileNotFoundError(2, "The system cannot find the file specified")
            return subprocess.CompletedProcess(
                argv, 1, b"", b"ERROR: The user name or password is incorrect.\r\n"
            )

        # Run the Windows escalation ladder on every platform. The POSIX branch
        # is deliberately untouched by this fix, so gating this test on os.name
        # would leave the regression unguarded on the host that reported it.
        monkeypatch.setattr(worker_supervisor.os, "name", "nt")
        monkeypatch.setattr(worker_supervisor.subprocess, "run", fake_run)

        started = time.monotonic()
        returncode = worker_supervisor._terminate_child(child)
        elapsed = time.monotonic() - started

        # The tree kill is still attempted first; it is only no longer trusted.
        assert [call[:2] for call in taskkill_calls] == [["taskkill", "/F"]]
        assert child.poll() is not None
        assert returncode is not None
        assert returncode == child.returncode
        assert elapsed < worker_supervisor.KILL_GRACE_SECONDS
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_terminate_child_raises_rather_than_returning_while_child_is_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A termination that failed outright must never look like a clean exit.

    supervise() turns the returned value into a terminal status artifact, so
    returning any int while the child still runs reports a dead worker that is
    in fact alive. The raise is what keeps that impossible.
    """

    class UnkillableChild:
        pid = 424242

        def __init__(self) -> None:
            self.kill_calls = 0

        def poll(self) -> None:
            return None

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("worker", timeout or 0)

        def kill(self) -> None:
            self.kill_calls += 1

    child = UnkillableChild()

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, b"", b"ERROR: Access is denied.\r\n")

    monkeypatch.setattr(worker_supervisor.os, "name", "nt")
    monkeypatch.setattr(worker_supervisor.subprocess, "run", fake_run)

    with pytest.raises(worker_supervisor.ChildTerminationError) as raised:
        worker_supervisor._terminate_child(child, grace=0.01)

    assert child.kill_calls == 1
    message = str(raised.value)
    assert "424242" in message
    # The diagnostic names the primitive that failed instead of hiding it.
    assert "taskkill" in message
    assert "Access is denied" in message


def test_terminate_child_kill_on_close_job_rung_ends_child_when_tree_kill_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NF-2026-01166 FINDING 1: the Job rung must actually run, and be trusted.

    child.kill() only reaches the direct child, not grandchildren, so when the
    tree kill fails the Job this child was assigned to has to be tried next,
    and trusted when it succeeds, rather than falling through to child.kill()
    regardless.
    """

    class StubJob:
        def __init__(self) -> None:
            self.terminate_calls = 0
            self.terminated = False

        def terminate(self) -> None:
            self.terminate_calls += 1
            self.terminated = True

    class StubChild:
        pid = 777777

        def __init__(self, job: StubJob) -> None:
            self.kill_calls = 0
            self._job = job
            setattr(self, worker_supervisor._KILL_JOB_ATTR, job)

        def poll(self):
            return 0 if self._job.terminated else None

        def wait(self, timeout=None):
            if self._job.terminated:
                return 0
            raise subprocess.TimeoutExpired("worker", timeout or 0)

        def kill(self) -> None:
            self.kill_calls += 1

    job = StubJob()
    child = StubChild(job)

    monkeypatch.setattr(worker_supervisor.os, "name", "nt")
    monkeypatch.setattr(
        worker_supervisor, "_windows_tree_kill", lambda pid, grace: "forced_failure"
    )

    returncode = worker_supervisor._terminate_child(child, grace=1.0)

    assert job.terminate_calls == 1
    assert returncode == 0
    assert child.kill_calls == 0


def test_terminate_child_raises_when_kill_on_close_job_terminate_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NF-2026-01166 FINDING 1: a Job that refuses to terminate must not be silent.

    The ladder falls through to child.kill() when the Job rung fails, but if
    the child still never dies the failure must surface as
    ChildTerminationError and name the rung that failed.
    """

    class FailingJob:
        def terminate(self) -> None:
            raise OSError("job handle is invalid")

    class StubChild:
        pid = 888888

        def __init__(self) -> None:
            self.kill_calls = 0
            setattr(self, worker_supervisor._KILL_JOB_ATTR, FailingJob())

        def poll(self):
            return None

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("worker", timeout or 0)

        def kill(self) -> None:
            self.kill_calls += 1

    child = StubChild()

    monkeypatch.setattr(worker_supervisor.os, "name", "nt")
    monkeypatch.setattr(
        worker_supervisor, "_windows_tree_kill", lambda pid, grace: "forced_failure"
    )

    with pytest.raises(worker_supervisor.ChildTerminationError) as raised:
        worker_supervisor._terminate_child(child, grace=0.01)

    assert child.kill_calls == 1
    assert "kill_on_close_job:" in str(raised.value)


@pytest.mark.skipif(os.name != "nt", reason="exercises real Win32 Job Object calls")
def test_terminate_child_kill_on_close_job_terminates_real_child(
    tmp_path: Path,
) -> None:
    """NF-2026-01166 FINDING 1: proves the TerminateJobObject ctypes signature.

    The stub-based tests above cover the ladder's control flow only; this is
    the one test that proves kernel32.TerminateJobObject's argtypes and the
    Job handle actually end a real assigned child.
    """
    child = _live_sleeping_child(tmp_path)
    job = worker_supervisor._WindowsKillOnCloseJob()
    try:
        job.assign(child)
        job.terminate()
        returncode = child.wait(timeout=worker_supervisor.KILL_GRACE_SECONDS)
        assert returncode is not None
    finally:
        job.close()
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_terminate_child_tree_kill_timeout_falls_back_and_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NF-2026-01166 FINDING 2: a hung taskkill must not block the ladder forever.

    `_windows_tree_kill` used to call subprocess.run with no timeout, so a
    taskkill that never returns would hang rung 1 forever and the later
    rungs -- and the ChildTerminationError guarantee -- were unreachable.
    """

    class UnkillableChild:
        pid = 909090

        def __init__(self) -> None:
            self.kill_calls = 0

        def poll(self):
            return None

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("worker", timeout or 0)

        def kill(self) -> None:
            self.kill_calls += 1

    child = UnkillableChild()

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    monkeypatch.setattr(worker_supervisor.os, "name", "nt")
    monkeypatch.setattr(worker_supervisor.subprocess, "run", fake_run)

    started = time.monotonic()
    with pytest.raises(worker_supervisor.ChildTerminationError) as raised:
        worker_supervisor._terminate_child(child, grace=0.01)
    elapsed = time.monotonic() - started

    assert child.kill_calls == 1
    assert "timeout" in str(raised.value)
    assert elapsed < 1.0
