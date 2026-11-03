"""NF-2026-01986: a timed-out declared quality check keeps a bounded output tail."""

from __future__ import annotations

import subprocess

from aiworkhub import quality_evidence as qe


_PRINT_THEN_SLEEP = (
    "import sys, time; "
    "print('stdout-before-timeout', flush=True); "
    "print('stderr-before-timeout', file=sys.stderr, flush=True); "
    "time.sleep(120)"
)


def test_timed_out_command_returns_stdout_and_stderr_tail(tmp_path) -> None:
    # Interpreter startup on a loaded host can exceed a short deadline before the
    # child prints anything, so a run with no output yet escalates the deadline.
    # Every attempt must still be a failed run carrying the timeout marker.
    for timeout_seconds in (2, 10, 30):
        status, stdout, stderr, duration = qe._run_command_array(
            ("{python}", "-I", "-S", "-c", _PRINT_THEN_SLEEP),
            cwd=tmp_path,
            timeout_seconds=timeout_seconds,
        )
        assert status == qe.STATUS_FAILED
        assert "timeout" in stderr
        assert f"limit {timeout_seconds}s" in stderr
        assert duration >= timeout_seconds
        if stdout:
            break

    assert "stdout-before-timeout" in stdout
    assert "stderr-before-timeout" in stderr
    assert stderr.rstrip().splitlines()[-1].startswith("timeout after ")


def test_timeout_tail_is_bounded_per_stream(monkeypatch, tmp_path) -> None:
    big = "x" * (qe.TIMEOUT_STREAM_TAIL_BYTES * 3) + "END"

    def fake_run(*_args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd="fake", timeout=kwargs.get("timeout"), output=big.encode(), stderr=big
        )

    monkeypatch.setattr(qe.subprocess, "run", fake_run)
    status, stdout, stderr, _duration = qe._run_command_array(
        ("{python}", "-c", "pass"), cwd=tmp_path, timeout_seconds=1
    )

    assert status == qe.STATUS_FAILED
    assert qe.TIMEOUT_STREAM_TAIL_BYTES == 8 * 1024
    assert len(stdout.encode()) <= qe.TIMEOUT_STREAM_TAIL_BYTES
    assert stdout.endswith("END")
    tail, marker = stderr.rsplit("\n", 1)
    assert len(tail.encode()) <= qe.TIMEOUT_STREAM_TAIL_BYTES
    assert tail.endswith("END")
    assert marker.startswith("timeout after ")


def test_timeout_tail_decodes_invalid_bytes_with_replacement() -> None:
    assert qe._timeout_stream_tail(None) == ""
    assert qe._timeout_stream_tail(b"ok\xff") == "ok�"
    assert qe._timeout_stream_tail("text") == "text"


def test_timeout_without_output_keeps_bare_marker(monkeypatch, tmp_path) -> None:
    def fake_run(*_args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="fake", timeout=kwargs.get("timeout"))

    monkeypatch.setattr(qe.subprocess, "run", fake_run)
    status, stdout, stderr, _duration = qe._run_command_array(
        ("{python}", "-c", "pass"), cwd=tmp_path, timeout_seconds=1
    )

    assert status == qe.STATUS_FAILED
    assert stdout == ""
    assert stderr.startswith("timeout after ")
