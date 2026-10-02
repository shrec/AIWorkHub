"""Hermetic coverage for NF-2026-01195: validation-only replay terminal state."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from aiworkhub import process_launcher, worker_workspace
from aiworkhub.process_launcher_validation import replay_terminal_state
from aiworkhub.worker_workspace import ValidationRunError
from test_process_launcher import _requires_anchored_reads


# --- pure function truth table ----------------------------------------------


@pytest.mark.parametrize(
    "terminal_state, execution_mode, declared, validations, expected",
    [
        # replay + declared commands + no receipts + validation_failed -> finalize_failed
        ("validation_failed", "validation_only_replay", ["cmd"], [], "finalize_failed"),
        # same, but a command already produced a receipt -> unchanged
        (
            "validation_failed",
            "validation_only_replay",
            ["cmd"],
            [{"command": "cmd", "returncode": 1}],
            "validation_failed",
        ),
        # non-replay -> unchanged
        ("validation_failed", "direct", ["cmd"], [], "validation_failed"),
        # zero declared commands -> unchanged
        ("validation_failed", "validation_only_replay", [], [], "validation_failed"),
        # other terminals are never touched
        ("scope_rejected", "validation_only_replay", ["cmd"], [], "scope_rejected"),
        ("promotion_conflict", "validation_only_replay", ["cmd"], [], "promotion_conflict"),
        ("finalize_failed", "validation_only_replay", ["cmd"], [], "finalize_failed"),
    ],
)
def test_replay_terminal_state_truth_table(
    terminal_state, execution_mode, declared, validations, expected
):
    metadata = {"execution_mode": execution_mode, "validation": declared}
    assert replay_terminal_state(terminal_state, metadata, validations) == expected


# --- finalizer-level wiring --------------------------------------------------


def _card() -> dict:
    return {
        "task_id": "TASK_NF01195",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "status": "processing",
        "worker_status": "claimed",
        "claimed_by": "claude_worker_b1",
    }


def _show(card: dict):
    def show(task_id: str) -> dict:
        assert task_id == card["task_id"]
        return {"returncode": 0, "stdout": json.dumps(card), "stderr": ""}

    return show


def _collision(**_kwargs):
    return {"returncode": 0, "stdout": '{"collision_free":true}', "stderr": ""}


def _plan(argv, repo):
    def adapter_builder(*_args, **_kwargs):
        return {"argv": argv, "cwd": str(repo)}

    return adapter_builder


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    )


def _build_manager(tmp_path, monkeypatch):
    monkeypatch.setenv("AIWORKHUB_TOOLCHAIN_AUTHORITY_HMAC_KEY", "hex:" + "11" * 32)
    monkeypatch.setenv(process_launcher.ALLOW_LAUNCH_ENV, "1")
    monkeypatch.setenv(process_launcher.ALLOW_WRITES_ENV, "1")

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "out").mkdir(parents=True)
    (repo / "out" / "result.json").write_text("canonical-v1", encoding="utf-8")

    workspace_dir = tmp_path / "workspace"
    (workspace_dir / "out").mkdir(parents=True)
    worked_file = workspace_dir / "out" / "result.json"
    worked_file.write_text("canonical-v1", encoding="utf-8")
    baseline_hash = worker_workspace._hash_path(worked_file)

    _git(workspace_dir, "init", "-q")
    _git(workspace_dir, "config", "user.email", "tests@example.invalid")
    _git(workspace_dir, "config", "user.name", "Task MCP Tests")
    _git(workspace_dir, "add", "out/result.json")
    _git(workspace_dir, "commit", "-qm", "baseline")

    home_dir = tmp_path / "home"
    home_dir.mkdir()

    workspace = worker_workspace.WorkerWorkspace(
        request_id="req-nf01195",
        repo=repo,
        path=workspace_dir,
        home=home_dir,
        allowed_writes=("out/result.json",),
        parent_baseline={"out/result.json": baseline_hash},
        workspace_baseline={"out/result.json": baseline_hash},
    )

    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=_show(_card()),
        collision_guard=_collision,
        adapter_builder=_plan([sys.executable, "-c", "pass"], repo),
        isolation_enabled=False,
    )
    monkeypatch.setattr(process_launcher, "promote", lambda *a, **k: [], raising=False)
    monkeypatch.setattr(
        process_launcher.core, "mark_review", lambda *a, **k: {"ok": True}
    )
    terminal_calls: list[tuple[str, str]] = []

    def _record(kind):
        def _terminal(_metadata, substatus, **_kwargs):
            terminal_calls.append((kind, substatus))
            return {"ok": True, "returncode": 0, "stdout": "{}", "stderr": ""}

        return _terminal

    monkeypatch.setattr(manager, "_terminal_failure_exact", _record("failure"))
    monkeypatch.setattr(manager, "_review_terminal_exact", _record("review"))
    return manager, workspace, terminal_calls


def _finalize(manager, workspace, tmp_path, request_id, *, extra_metadata):
    stdout_path = tmp_path / f"{request_id}.stdout.log"
    stderr_path = tmp_path / f"{request_id}.stderr.log"
    stdout_path.write_text("worker complete\n", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    status_path = tmp_path / f"{request_id}.supervisor.json"
    metadata_path = tmp_path / f"{request_id}.request.json"
    worker_workspace.write_json_0600(status_path, {"state": "exited", "exit_code": 0})
    metadata = {
        "schema_id": "aiworkhub.task_mcp.isolated_request.v1",
        "request_id": request_id,
        "task_id": "TASK_NF01195",
        "runner": "claude_worker_b1",
        "topic": "task_mcp",
        "adapter_id": "claude_cli",
        "model": "claude_cli",
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "supervisor_status_path": str(status_path),
        "cancel_path": str(tmp_path / f"{request_id}.cancel.json"),
        "prompt_sha256": "0" * 64,
        "project_context": None,
        "project_context_delivery": {"injected": False},
        "sandbox_backend": "landlock",
        "required_outputs": [],
        "allow_empty_required_outputs": [],
        "allow_unchanged_required_outputs": [],
        "external_readonly_dirs": [],
        "workspace": workspace.as_metadata(),
        "claim_epoch": 1,
    }
    metadata.update(extra_metadata)
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    manager._append_event(
        {
            "request_id": request_id,
            "task_id": "TASK_NF01195",
            "runner": "claude_worker_b1",
            "topic": "task_mcp",
            "adapter_id": "claude_cli",
            "model": "claude_cli",
            "state": "running",
            "pid": 999_999_999,
            "pid_start_ticks": 1,
            "metadata_path": str(metadata_path),
            "supervisor_status_path": str(status_path),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }
    )
    return manager._finalize_isolated_request(request_id, supervisor_returncode=0)


@_requires_anchored_reads
def test_replay_finalize_error_before_any_command_runs_terminalises_finalize_failed(
    monkeypatch, tmp_path
):
    manager, workspace, terminal_calls = _build_manager(tmp_path, monkeypatch)

    event = _finalize(
        manager,
        workspace,
        tmp_path,
        "req-nf01195-b",
        extra_metadata={
            "execution_mode": "validation_only_replay",
            "validation": ["true"],
            "required_outputs": ["out/result.json"],
        },
    )
    assert event["state"] == "finalize_failed"
    assert '"unchanged_mandatory_outputs":["out/result.json"]' in event["error"]
    assert event["validation"] == []
    assert terminal_calls == [("failure", "finalize_failed")]


@_requires_anchored_reads
def test_replay_failed_declared_command_keeps_validation_failed_with_receipt(
    monkeypatch, tmp_path
):
    manager, workspace, terminal_calls = _build_manager(tmp_path, monkeypatch)

    def _raise_validation_run_error(*_args, **_kwargs):
        raise ValidationRunError(
            "validation_command_failed",
            [{"command": "fail-cmd", "returncode": 1}],
        )

    monkeypatch.setattr(
        process_launcher, "_run_declared_validations", _raise_validation_run_error
    )

    event = _finalize(
        manager,
        workspace,
        tmp_path,
        "req-nf01195-c",
        extra_metadata={
            "execution_mode": "validation_only_replay",
            "validation": ["fail-cmd"],
        },
    )
    assert event["state"] == "validation_failed"
    assert event["validation"] == [{"command": "fail-cmd", "returncode": 1}]
    assert terminal_calls == [("review", "validation_failed")]


@_requires_anchored_reads
def test_replay_finalize_error_stays_validation_failed_when_replay_terminal_state_is_identity(
    monkeypatch, tmp_path
):
    manager, workspace, terminal_calls = _build_manager(tmp_path, monkeypatch)
    monkeypatch.setattr(
        process_launcher._launcher_validation,
        "replay_terminal_state",
        lambda state, _metadata, _validations: state,
    )

    event = _finalize(
        manager,
        workspace,
        tmp_path,
        "req-nf01195-d",
        extra_metadata={
            "execution_mode": "validation_only_replay",
            "validation": ["true"],
            "required_outputs": ["out/result.json"],
        },
    )
    assert event["state"] == "validation_failed"
    assert terminal_calls == [("review", "validation_failed")]
