"""NF-2026-02096: a validation command killed at its deadline stops the batch.

A declared command that hit its per-command deadline (returncode ``None``,
``timed_out``) used to be followed by the next declared step, which then ran on
the half-built tree; the batch was recorded ``validation_failed`` as candidate
code and only a full worker relaunch recovered. Now every later declared step is
reported ``skipped_after_timeout`` without being launched, and the batch carries
the runner-measured ``validation_command_timed_out`` signature, so the finalizer
routes it to the validation-only replay instead of a relaunch.

Fakes only: no real provider CLI and no real slow process.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import build_lane, worker_workspace
from aiworkhub import terminal_failure_classification as tfc
from aiworkhub import validation_runner

_TASK_ID = "TASK_NF02096"
_OUTPUT = "src/parse.py"
_DIGEST = "file:644:" + "ab" * 32
_SIGNATURE = "validation_command_timed_out"
_SLOW = "python3 -c '__import__(\"time\").sleep(3600)'"
_MARKER_COMMAND = "python3 -c 'open(\"marker.txt\", \"w\").write(\"ran\")'"
_TINY_TIMEOUT = 1


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=False
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "parent"
    root.mkdir()
    assert _git(root, "init", "-q").returncode == 0
    assert _git(root, "config", "user.email", "tests@example.invalid").returncode == 0
    assert _git(root, "config", "user.name", "Task MCP Tests").returncode == 0
    (root / "read").mkdir()
    (root / "out").mkdir()
    (root / "read" / "input.txt").write_bytes(b"input-v1\n")
    (root / "out" / "result.txt").write_bytes(b"result-v1\n")
    assert _git(root, "add", "read/input.txt", "out/result.txt").returncode == 0
    assert _git(root, "commit", "-qm", "fixture").returncode == 0
    return root


@pytest.fixture
def workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, repo: Path):
    monkeypatch.setenv(worker_workspace.WORKTREE_ROOT_ENV, str(tmp_path / "worktrees"))
    created = worker_workspace.create_workspace(
        repo,
        "nf02096-timeout",
        {"allowed_writes": ["out/result.txt"], "read_first": ["read/input.txt"]},
        "validation",
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(worker_workspace, "select_sandbox_backend", lambda: "landlock")
    monkeypatch.setattr(
        worker_workspace, "provision_validation_exec_scratch", lambda _workspace, **_: scratch
    )
    monkeypatch.setattr(worker_workspace, "cleanup_validation_exec_scratch", lambda _path: None)
    monkeypatch.setattr(
        worker_workspace,
        "sandbox_argv",
        lambda _workspace, _adapter, argv, **_kwargs: list(argv),
    )
    monkeypatch.setattr(worker_workspace, "sanitized_env", lambda *_args, **_kwargs: {})
    try:
        yield created
    finally:
        worker_workspace.cleanup_workspace(repo, created.path, created.home)


class _FakeRun:
    """``subprocess.run`` stand-in: the slow command overruns any tiny deadline;
    the marker command writes the marker file it would write for real."""

    def __init__(self, marker: Path) -> None:
        self.marker = marker
        self.calls: list[dict[str, Any]] = []

    def __call__(self, argv: Any, *_args: object, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append({"argv": list(argv), "timeout": kwargs.get("timeout")})
        if any("sleep(3600)" in str(token) for token in argv):
            timeout = kwargs.get("timeout")
            assert timeout is not None and timeout <= _TINY_TIMEOUT
            raise subprocess.TimeoutExpired(list(argv), timeout, output=b"[ 42%] Building", stderr=None)
        self.marker.write_text("ran", encoding="utf-8")
        return subprocess.CompletedProcess(list(argv), 0, "ok", "")


def _assert_skipped(row: dict[str, Any], command: str, timed_out_command: str) -> None:
    assert row["command"] == command
    assert row["declared_command"] == command
    assert row["skipped_after_timeout"] is True
    assert row["timed_out"] is False
    assert row["returncode"] is None
    assert row["skipped_reason"] == f"previous_command_timed_out:{timed_out_command}"
    assert "python_candidate_authority" in row


# --- (1) the runner stops after a timed-out command ------------------------- #


def test_a_timed_out_command_skips_every_later_declared_step(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, workspace
) -> None:
    marker = tmp_path / "marker.txt"
    fake = _FakeRun(marker)
    monkeypatch.setattr(worker_workspace.subprocess, "run", fake)
    commands = [_SLOW, _MARKER_COMMAND]

    with pytest.raises(worker_workspace.ValidationRunError) as caught:
        worker_workspace.run_validations(workspace, commands, timeout_seconds=_TINY_TIMEOUT)

    rows = caught.value.results
    assert [row["command"] for row in rows] == commands
    assert rows[0]["timed_out"] is True
    assert rows[0]["returncode"] is None
    assert rows[0]["stdout_tail"] == "[ 42%] Building"  # decode_timeout_stream
    assert rows[0].get("skipped_after_timeout") is not True
    _assert_skipped(rows[1], _MARKER_COMMAND, _SLOW)
    # Every per-run key consumers read of every row is carried.
    assert rows[1]["python_candidate_authority"] == rows[0]["python_candidate_authority"]
    assert rows[1]["sandbox_backend"] == rows[0]["sandbox_backend"]
    assert len(fake.calls) == 1
    assert not marker.exists()
    assert str(caught.value).startswith("validation_timeout:") or "stalled" in rows[0]


def test_a_timed_out_lane_row_skips_every_later_declared_step(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, workspace
) -> None:
    marker = tmp_path / "marker.txt"
    fake = _FakeRun(marker)
    monkeypatch.setattr(worker_workspace.subprocess, "run", fake)
    lane_command = "lane:cmake-build"
    lane_calls: list[str] = []

    def _lane(_workspace: object, command: str) -> dict[str, Any]:
        lane_calls.append(command)
        return {
            "command": command,
            "declared_command": command,
            "sandbox_backend": "docker",
            "execution_boundary": "docker_container_lane",
            "python_candidate_authority": None,
            "returncode": None,
            "timed_out": True,
            "timeout_seconds": _TINY_TIMEOUT,
            "stdout_tail": "",
            "stderr_tail": "",
        }

    monkeypatch.setattr(build_lane, "run_lane_validation", _lane)
    commands = [lane_command, _MARKER_COMMAND, "lane:cmake-test"]

    with pytest.raises(worker_workspace.ValidationRunError) as caught:
        worker_workspace.run_validations(workspace, commands, timeout_seconds=_TINY_TIMEOUT)

    rows = caught.value.results
    assert [row["command"] for row in rows] == commands
    assert rows[0]["timed_out"] is True
    _assert_skipped(rows[1], _MARKER_COMMAND, lane_command)
    _assert_skipped(rows[2], "lane:cmake-test", lane_command)
    assert rows[1]["sandbox_backend"] == "docker"
    assert lane_calls == [lane_command]
    assert fake.calls == []
    assert not marker.exists()


def test_skipped_rows_helper_never_passes_and_decode_handles_every_stream() -> None:
    carried = {"sandbox_backend": "landlock", "python_candidate_authority": {"digest": "d"}}
    rows = validation_runner.skipped_after_timeout_rows(
        ["a", "b"], timed_out_command="slow", carried=carried
    )
    assert [row["command"] for row in rows] == ["a", "b"]
    for row in rows:
        _assert_skipped(row, row["command"], "slow")
        assert row["python_candidate_authority"] == {"digest": "d"}
        assert validation_runner.classify_validation_results([row]).state != (
            validation_runner.VALIDATION_PASSED
        )
    assert validation_runner.skipped_after_timeout_rows([], timed_out_command="slow") == []
    assert validation_runner.decode_timeout_stream(b"x\xff") == "x�"
    assert validation_runner.decode_timeout_stream("y") == "y"
    assert validation_runner.decode_timeout_stream(None) == ""


# --- (2) the batch carries the runner-measured signature -------------------- #


def _timed_out_row(command: str = "cmake --build build") -> dict[str, Any]:
    return {
        "command": command,
        "declared_command": command,
        "sandbox_backend": "landlock",
        "returncode": None,
        "timed_out": True,
        "timeout_seconds": 1818,
        "stdout_head": "[ 42%] Building CXX object",
        "stdout_tail": "[ 42%] Building CXX object",
        "stdout_truncated": False,
        "stderr_head": "",
        "stderr_tail": "",
        "stderr_truncated": False,
    }


def _skipped_row(command: str = "ctest --test-dir build") -> dict[str, Any]:
    timed_out = _timed_out_row()
    return validation_runner.skipped_after_timeout_rows(
        [command], timed_out_command=timed_out["command"], carried=timed_out
    )[0]


def _failing_row() -> dict[str, Any]:
    return {
        "command": "ctest --test-dir build",
        "declared_command": "ctest --test-dir build",
        "sandbox_backend": "landlock",
        "returncode": 8,
        "timed_out": False,
        "stdout_head": "1 test failed",
        "stdout_tail": "1 test failed",
        "stdout_truncated": False,
        "stderr_head": "",
        "stderr_tail": "",
        "stderr_truncated": False,
    }


def _records() -> list[dict[str, Any]]:
    return [
        {"pattern": _OUTPUT, "path": _OUTPUT, "bytes": 42, "sha256": _DIGEST,
         "unchanged_allowed": False}
    ]


def _match(rows: list[dict[str, Any]], records: Any = None):
    return validation_runner.match_validation_environment_signature(
        rows,
        required_outputs=[_OUTPUT],
        required_output_records=_records() if records is None else records,
    )


def test_timed_out_and_skipped_rows_match_the_timeout_signature() -> None:
    match = _match([{"command": "ruff check src", "returncode": 0}, _timed_out_row(), _skipped_row()])
    assert match is not None
    assert match.signature == _SIGNATURE == validation_runner.VALIDATION_TIMEOUT_SIGNATURE
    assert match.command == "cmake --build build"
    assert match.sandbox_backend == "landlock"


def test_a_real_nonzero_exit_row_keeps_the_batch_plain() -> None:
    assert _match([_timed_out_row(), _failing_row()]) is None
    assert _match([_failing_row(), _timed_out_row()]) is None


def test_required_outputs_must_still_be_hash_recorded() -> None:
    assert _match([_timed_out_row(), _skipped_row()], records=[]) is None
    assert _match(
        [_timed_out_row(), _skipped_row()], records=[{**_records()[0], "sha256": None}]
    ) is None


def test_candidate_text_never_forges_the_timeout_signature() -> None:
    forged = {**_failing_row(), "stdout_tail": f"{_SIGNATURE} skipped_after_timeout"}
    assert _match([forged]) is None
    entry = next(
        signature
        for signature in validation_runner.VALIDATION_ENVIRONMENT_SIGNATURES
        if signature.name == _SIGNATURE
    )
    assert entry.literals == () and entry.backends == ()
    assert entry.candidate_forgeable is False
    assert entry.evidence_source == validation_runner.LANE_INFRASTRUCTURE_EVIDENCE_SOURCE
    assert entry.lane_change_reason == "validation_command_deadline_exceeded_measured"
    assert validation_runner.validation_environment_signature(_timed_out_row()) is None


def test_a_lane_failure_class_keeps_priority_over_timed_out() -> None:
    lane_row = {
        **_timed_out_row("lane:cmake-build"),
        "failure_class": build_lane.VALIDATION_ENVIRONMENT_CLASS,
    }
    match = _match([lane_row])
    assert match is not None
    assert match.signature == validation_runner.LANE_INFRASTRUCTURE_SIGNATURE


# --- (3) the finalizer routes it to the validation-only replay -------------- #


def test_the_disposition_routes_a_timeout_to_the_validation_only_replay() -> None:
    disposition = tfc.validation_environment_disposition(
        terminal_state="validation_failed",
        validations=[_timed_out_row(), _skipped_row()],
        required_outputs=[_OUTPUT],
        required_output_records=_records(),
        task_id=_TASK_ID,
    )

    assert disposition is not None
    assert disposition["cause"] == tfc.CAUSE_VALIDATION_ENVIRONMENT
    assert disposition["evidence"] == f"validation_environment_signature={_SIGNATURE}"
    assert disposition["candidate_forgeable"] is False
    assert disposition["provider_launched"] is False
    recovery = disposition["validation_environment_recovery"]
    assert recovery["signature"] == _SIGNATURE
    assert recovery["command"] == "cmake --build build"
    assert recovery["lane_change_reason"] == "validation_command_deadline_exceeded_measured"
    plan = recovery["plan"]
    assert [step["tool"] for step in plan] == [
        "aiworkhub_task_reject_review",
        "aiworkhub_task_recover_blocked_rework",
    ]
    assert plan[0]["arguments"]["to"] == "blocked"
    assert plan[0]["arguments"]["failure_category"] == "validation_environment"
    assert plan[-1]["arguments"] == {"task_id": _TASK_ID, "validation_only_replay": True}


def test_signatures_version_is_unchanged() -> None:
    assert validation_runner.VALIDATION_ENVIRONMENT_SIGNATURES_VERSION == (
        "aiworkhub.validation_environment_signatures.v1"
    )
