"""NF-2026-02024: output landing after the last heartbeat reaches the terminal status."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from aiworkhub import worker_supervisor
from aiworkhub.worker_workspace import write_json_0600


def _spec(tmp_path: Path, argv: list[str]) -> tuple[Path, dict]:
    process_dir = tmp_path / "processes"
    process_dir.mkdir(mode=0o700)
    spec_path = process_dir / "request.spec.json"
    payload = {
        "argv": argv,
        "cwd": str(tmp_path),
        "timeout_seconds": 30,
        "status_path": str(process_dir / "status.json"),
        "cancel_path": str(process_dir / "cancel.json"),
        "stdout_path": str(process_dir / "stdout.log"),
        "stderr_path": str(process_dir / "stderr.log"),
    }
    write_json_0600(spec_path, payload)
    return spec_path, payload


def _child_script(status_path: Path) -> str:
    # The child itself waits for the supervisor's first in-loop heartbeat
    # (heartbeat_seq >= 1, published once at loop start) before printing its
    # usage line and exiting. The next heartbeat is an hour away, so the usage
    # line can only be classified by the post-join observation -- ordering is
    # established by the observed status artifact, never by a sleep.
    return (
        "import json, os, sys, time\n"
        f"status_path = {str(status_path)!r}\n"
        "deadline = time.monotonic() + 20.0\n"
        "while True:\n"
        "    try:\n"
        "        with open(status_path, encoding='utf-8') as handle:\n"
        "            seq = int(json.load(handle).get('heartbeat_seq') or 0)\n"
        "    except (OSError, ValueError):\n"
        "        seq = 0\n"
        "    if seq >= 1:\n"
        "        break\n"
        "    if time.monotonic() >= deadline:\n"
        "        os._exit(3)\n"
        "    time.sleep(0.01)\n"
        "sys.stdout.write(json.dumps({'usage': {'input_tokens': 9, 'output_tokens': 4}}) + '\\n')\n"
        "sys.stdout.flush()\n"
        "os._exit(0)\n"
    )


@pytest.mark.parametrize("adapter_id", ["vscode_lm", "deepseek_vscode_lm"])
def test_usage_after_last_heartbeat_reaches_terminal_status(
    tmp_path: Path, adapter_id: str,
) -> None:
    status_path = tmp_path / "processes" / "status.json"
    spec_path, spec = _spec(tmp_path, [sys.executable, "-c", _child_script(status_path)])
    assert Path(spec["status_path"]) == status_path
    spec.update(adapter_id=adapter_id, heartbeat_interval_seconds=3600)
    write_json_0600(spec_path, spec)

    result = subprocess.run(
        [sys.executable, str(Path(worker_supervisor.__file__)), "--spec", str(spec_path)],
        cwd=str(tmp_path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        shell=False,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    status = json.loads(status_path.read_text(encoding="utf-8"))
    assert status["state"] == "exited"
    assert status["exit_code"] == 0
    # Exactly the one start-of-loop heartbeat ran: the usage line post-dates it.
    assert status["heartbeat_seq"] == 1
    assert status["last_meaningful_phase"] == "provider_usage"
    assert status["last_meaningful_progress_epoch"] > status["started_at_epoch"]
    # Usage growth never moves the actionable clock.
    assert status["last_actionable_progress_epoch"] is None
    budget = status["token_budget"]
    assert budget["accepted_total_tokens"] == 13
    # First seen only after the child exited: post-hoc, never enforced-live.
    assert budget["enforceable_live_tokens"] == 0
    assert budget["events"][-1]["cap_enforceable"] is False
