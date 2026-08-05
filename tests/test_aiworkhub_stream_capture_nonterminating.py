"""Captured provider bytes are telemetry, never execution authority."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import aiworkhub.worker_supervisor as ws


TERMINAL_MARKER = "__WORKER_DONE_MARKER__"


def _run(tmp_path: Path, spec_overrides: dict) -> tuple[int, dict]:
    status_path = tmp_path / "status.json"
    spec = {
        "cwd": str(tmp_path),
        "timeout_seconds": 60,
        "status_path": str(status_path),
        "cancel_path": str(tmp_path / "cancel"),
        "stdout_path": str(tmp_path / "stdout.log"),
        "stderr_path": str(tmp_path / "stderr.log"),
    }
    spec.update(spec_overrides)
    rc = ws.supervise(spec)
    return rc, json.loads(status_path.read_text())


def test_verbose_stream_beyond_total_threshold_exits_normally(tmp_path: Path) -> None:
    chunk = "x" * 4096
    program = (
        "import sys\n"
        f"for _ in range(2000):\n"
        f"    sys.stdout.write({chunk!r})\n"
        "    sys.stdout.flush()\n"
        f"sys.stdout.write({TERMINAL_MARKER!r} + '\\n')\n"
        "sys.stdout.flush()\n"
    )
    rc, status = _run(
        tmp_path,
        {
            "argv": [sys.executable, "-c", program],
            "max_output_bytes": ws.MIN_MAX_OUTPUT_BYTES,
            "max_total_output_bytes": ws.MIN_MAX_OUTPUT_BYTES,
            "heartbeat_interval_seconds": 0.05,
        },
    )

    assert rc == 0, status
    assert status["state"] == "exited"
    assert status["exit_code"] == 0
    tail = (tmp_path / "stdout.log").read_text()
    assert len(tail.encode()) <= ws.MIN_MAX_OUTPUT_BYTES
    assert status["stdout_dropped_bytes"] > 0
    assert TERMINAL_MARKER in tail
    output_budget = status["output_budget"]
    assert output_budget["byte_labels_are_token_truth"] is False
    assert output_budget["observed_bytes"] > output_budget["cap_bytes"]
    assert status["error"] == ""


def test_no_byte_count_only_termination_branch_in_source() -> None:
    source = Path(ws.__file__).read_text()
    assert "observed_output_bytes" not in source
    assert 'final_state = "output_budget_exceeded"' not in source
    assert "return 121" not in source
