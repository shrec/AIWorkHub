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
    return json.loads(path.read_text(encoding="utf-8"))


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
    assert status["last_meaningful_phase"] == "final_edit"


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


def test_supervisor_timeout_kills_child_and_persists_timeout(tmp_path: Path) -> None:
    spec_path, spec = _spec(
        tmp_path,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        timeout=1,
    )
    started = time.monotonic()
    result = _run_supervisor(spec_path)
    assert result.returncode == 124
    assert time.monotonic() - started < 5
    status = _read_status(Path(spec["status_path"]))
    assert status["state"] == "timed_out"
    assert status["exit_code"] != 0


def test_supervisor_enforces_provider_reported_live_token_cap(tmp_path: Path) -> None:
    script = (
        "import json,time; "
        "print(json.dumps({'usage': {'input_tokens': 9, 'output_tokens': 4}}), flush=True); "
        "time.sleep(30)"
    )
    spec_path, spec = _spec(tmp_path, [sys.executable, "-c", script])
    spec.update(
        adapter_id="vscode_lm",
        token_budget={"cap_tokens": 10},
        heartbeat_interval_seconds=0.05,
    )
    write_json_0600(spec_path, spec)

    result = _run_supervisor(spec_path)

    assert result.returncode == 122, result.stderr.decode()
    status = _read_status(Path(spec["status_path"]))
    assert status["state"] == "token_budget_exceeded"
    assert status["token_budget"]["cap_tokens"] == 10
    assert status["token_budget"]["enforceable_live_tokens"] == 13
    assert status["token_budget"]["events"][-1]["cap_enforceable"] is True


def test_supervisor_sums_claude_completed_turn_usage_for_live_cap(
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
        "print(json.dumps(event), flush=True); time.sleep(.2); "
        "print(json.dumps(event), flush=True); time.sleep(30)"
    )
    spec_path, spec = _spec(tmp_path, [sys.executable, "-c", script])
    spec.update(
        adapter_id="claude_cli",
        token_budget={"cap_tokens": 100},
        heartbeat_interval_seconds=0.05,
    )
    write_json_0600(spec_path, spec)

    result = _run_supervisor(spec_path)

    assert result.returncode == 122, result.stderr.decode()
    status = _read_status(Path(spec["status_path"]))
    assert status["state"] == "token_budget_exceeded"
    assert status["token_budget"]["enforceable_live_tokens"] == 112
    assert status["token_budget"]["events"][-1]["cap_enforceable"] is True


def test_supervisor_does_not_terminate_on_output_bytes(
    tmp_path: Path,
) -> None:
    # Byte-count telemetry must not stop the child. This fixture deliberately
    # sleeps after emitting more than the threshold, so timeout is the sole
    # authoritative terminal reason.
    script = (
        "import sys,time; "
        "sys.stdout.write('x' * 4096); sys.stdout.flush(); time.sleep(30)"
    )
    spec_path, spec = _spec(tmp_path, [sys.executable, "-c", script])
    spec.update(
        max_total_output_bytes=2048,
        heartbeat_interval_seconds=0.05,
        timeout_seconds=1,
    )
    write_json_0600(spec_path, spec)

    result = _run_supervisor(spec_path)

    assert result.returncode == 124, result.stderr.decode()
    status = _read_status(Path(spec["status_path"]))
    assert status["state"] == "timed_out"
    assert status["state"] != "output_budget_exceeded"
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
    assert status["token_budget"]["accepted_total_tokens"] == 13
    assert status["token_budget"]["enforceable_live_tokens"] == 0
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
        start_new_session=True,
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

    cancel_path = Path(spec["cancel_path"])
    write_json_0600(cancel_path, {"reason": "test-restart-cancel"})
    if os.name != "nt":
        os.kill(process.pid, signal.SIGTERM)
    assert process.wait(timeout=5) == 125
    final = _read_status(status_path)
    assert final["state"] == "cancelled"
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
        start_new_session=True,
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
        except FileNotFoundError:
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


def test_posix_worker_spawn_kwargs_are_platform_specific() -> None:
    linux = worker_supervisor._posix_worker_spawn_kwargs("linux")
    macos = worker_supervisor._posix_worker_spawn_kwargs("darwin")

    assert linux["start_new_session"] is True
    assert linux["preexec_fn"] is worker_supervisor._die_with_supervisor
    assert macos == {"start_new_session": True}
