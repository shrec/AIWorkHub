"""NF-2026-01822: a Windows AppContainer worker's validation_run really runs.

``generate_worker_mcp_runtime`` creates the server's private runtime directory
as ``<request HOME>/task_mcp_worker_runtime`` and the AppContainer bridge copies
the contract packet into it, so the HOME the packet resolves to IS that private
directory.  ``_contained_validation_blocker`` rightly refuses such a HOME, so
every contained worker got ``validation_run_unavailable_outside_the_worker_sandbox``
(requests 1f60e076a9724f30a4ed5901f9e4c176, 2bd901961ee04c03a6eac499ec8a6e97).
The lane now runs with a fresh HOME beside the private directory; the blocker
stays the guard for any HOME that would still expose it.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import windows_appcontainer  # noqa: E402
from aiworkhub import worker_ai_tools_mcp as w  # noqa: E402
from aiworkhub import worker_workspace  # noqa: E402

REQUEST_ID = "req-nf01822"
REFUSAL = "validation_run_unavailable_outside_the_worker_sandbox"
METRIC = "AIWORKHUB_METRIC name=collected value=1"


@pytest.fixture(autouse=True)
def _isolated_memo() -> None:
    w._validation_memo_reset()
    yield
    w._validation_memo_reset()


@pytest.fixture(autouse=True)
def _mute_chmod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
    if hasattr(os, "fchmod"):
        monkeypatch.setattr(os, "fchmod", lambda *a, **k: None)


def _write_packet(home: Path, validation: list[str]) -> Path:
    payload = {
        "schema_id": worker_workspace.WORKER_CONTRACT_PACKET_SCHEMA,
        "request_id": REQUEST_ID,
        "allowed_writes": [],
        "required_outputs": [],
        "allow_empty_required_outputs": [],
        "allow_unchanged_required_outputs": [],
        "validation": validation,
        "validation_roles": [],
        "read_only": False,
        "parent_baseline": {},
        "workspace_baseline": {},
        "inherited_rework_paths": [],
        "residual_contract_manifest": [],
    }
    path = worker_workspace.worker_contract_packet_path(home)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _production_ctx(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, validation: list[str]
) -> tuple[w.WorkerToolContext, Path, Path]:
    """The bridged server's context exactly as the supervisor binds it."""
    request = tmp_path / REQUEST_ID
    home = request / "home"
    home.mkdir(parents=True)
    worktree = request / "worktree"
    (worktree / "tests").mkdir(parents=True)
    (worktree / "emit.py").write_text(
        f"print({METRIC!r})\nprint('1 passed in 0.01s')\n", encoding="utf-8"
    )
    packet = _write_packet(home, validation)
    runtime = w.generate_worker_mcp_runtime(
        home=home, request_id=REQUEST_ID, task_id="T", runner="r", topic="t",
        repo=worktree, authority_repo=tmp_path, source_graph_targets=[],
        session_topic="s", package_import_root=tmp_path, python_executable="py.exe",
        contract_packet_path=packet,
    )
    # The pipe shim is Windows PowerShell; only the shim argv is stubbed, so the
    # real bridge still copies the packet and binds the contained markers.
    monkeypatch.setattr(windows_appcontainer, "new_worker_pipe_name", lambda rid: f"pipe-{rid}")
    monkeypatch.setattr(
        windows_appcontainer, "worker_pipe_shim_argv", lambda pipe, relay=None: ["shim", pipe]
    )
    _config, bridge = w.appcontainer_mcp_bridge(
        runtime, request_id=REQUEST_ID, home=home, stderr_path=tmp_path / "mcp.err"
    )
    ctx = w.load_context_from_env(bridge["env"])
    runtime_dir = runtime.audit_ledger_path.parent.resolve()
    assert runtime_dir == (home / "task_mcp_worker_runtime").resolve()
    return ctx, runtime_dir, worktree


def _container_row(command: str, stdout: str) -> dict[str, object]:
    return {
        "command": command,
        "declared_argv": ["python3", "emit.py"],
        "executed_argv": ["python.exe", "-P", "emit.py"],
        "returncode": 0,
        "duration_seconds": 0.25,
        "sandbox_backend": worker_workspace.WINDOWS_APPCONTAINER_BACKEND,
        "stdout_tail": stdout,
        "stderr_tail": "",
    }


def _never(*args: object, **kwargs: object) -> None:
    raise AssertionError("no host subprocess may run for a contained worker")


def test_the_packet_home_is_the_private_runtime_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The reproduction: the HOME the old lane used is refused on every run."""
    ctx, runtime_dir, _worktree = _production_ctx(monkeypatch, tmp_path, ["python3 emit.py"])
    assert (ctx.contained_worker, ctx.contained_worker_adapter) == (
        worker_workspace.WINDOWS_APPCONTAINER_BACKEND, "claude_cli",
    )
    packet_workspace = w._packet_workspace(ctx, w._contract_packet(ctx))
    assert Path(packet_workspace.home).resolve() == runtime_dir
    assert "private runtime directory" in w._contained_validation_blocker(ctx, packet_workspace)


def test_production_layout_reaches_the_contained_lane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ctx, runtime_dir, worktree = _production_ctx(monkeypatch, tmp_path, ["python3 emit.py"])
    calls: list[tuple[Path, list[str], dict[str, object]]] = []

    def _run_validations(workspace, commands, **route):
        home = Path(workspace.home)
        assert home.is_dir() and not any(home.iterdir())
        calls.append((home, list(commands), route))
        return [_container_row(commands[0], f"{METRIC}\n1 passed in 0.01s\n")]

    monkeypatch.setattr(worker_workspace, "run_validations", _run_validations)
    monkeypatch.setattr(w, "_run_one_validation", _never)

    result = w.validation_run(ctx, index="all")

    assert result.get("reason") != REFUSAL
    assert result["ok"] is True
    assert result["all_passed"] is True
    [(home, commands, route)] = calls
    assert commands == ["python3 emit.py"]
    assert route["backend"] == worker_workspace.WINDOWS_APPCONTAINER_BACKEND
    assert route["adapter_id"] == "claude_cli_validation"
    # Neither the private directory nor any ancestor of it, nor inside it.
    assert home != runtime_dir
    assert home not in runtime_dir.parents
    assert runtime_dir not in home.parents
    assert home.parent == runtime_dir.parent
    assert not home.exists()
    row = result["results"][0]
    assert row["returncode"] == 0
    assert row["candidate_bytes_sha256"] == result["candidate_bytes_sha256"]
    assert METRIC in row["diagnostic_tail"]
    assert Path(row["full_output_path"]).resolve().parent.parent == runtime_dir
    assert worktree.is_dir()


@pytest.mark.parametrize("where", ["runtime_dir", "request_home", "request_dir"])
def test_a_home_at_or_above_the_private_directory_is_still_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, where: str
) -> None:
    ctx, runtime_dir, _worktree = _production_ctx(monkeypatch, tmp_path, ["python3 emit.py"])
    home = {
        "runtime_dir": runtime_dir,
        "request_home": runtime_dir.parent,
        "request_dir": runtime_dir.parent.parent,
    }[where]
    workspace = dataclasses.replace(
        w._packet_workspace(ctx, w._contract_packet(ctx)), home=home
    )
    reason = w._contained_validation_blocker(ctx, workspace)
    assert reason == (
        f"the validation HOME {home.resolve()} would grant candidate code this "
        f"server's private runtime directory {runtime_dir}"
    )

    # And validation_run still refuses when the lane HOME would expose it.
    monkeypatch.setattr(
        w, "_contained_validation_workspace", lambda _ctx, _ws: contextlib.nullcontext(workspace)
    )
    monkeypatch.setattr(worker_workspace, "run_validations", _never)
    monkeypatch.setattr(w, "_run_one_validation", _never)
    result = w.validation_run(ctx, index="all")
    assert result["ok"] is False
    assert result["reason"] == REFUSAL
    assert reason in result["detail"]


def test_a_fresh_lane_home_passes_the_blocker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ctx, _runtime_dir, _worktree = _production_ctx(monkeypatch, tmp_path, ["python3 emit.py"])
    workspace = w._packet_workspace(ctx, w._contract_packet(ctx))
    with w._contained_validation_workspace(ctx, workspace) as lane:
        assert lane.path == workspace.path
        assert lane.allowed_writes == workspace.allowed_writes
        assert w._contained_validation_blocker(ctx, lane) == ""
        lane_home = Path(lane.home)
        assert lane_home.is_dir()
    assert not lane_home.exists()


def test_contained_rows_have_the_uncontained_row_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ctx, _runtime_dir, _worktree = _production_ctx(monkeypatch, tmp_path, ["python3 emit.py"])
    monkeypatch.setattr(
        worker_workspace,
        "run_validations",
        lambda workspace, commands, **route: [
            _container_row(commands[0], f"{METRIC}\n1 passed in 0.01s\n")
        ],
    )
    contained = w.validation_run(ctx, index=0)["results"][0]

    w._validation_memo_reset()
    host_ctx = dataclasses.replace(ctx, contained_worker="", contained_worker_adapter="")
    host = w.validation_run(host_ctx, index=0)["results"][0]

    assert host["returncode"] == 0, host
    assert set(contained) - {"sandbox_backend"} == set(host)
    assert contained["candidate_bytes_sha256"] == host["candidate_bytes_sha256"]
    assert contained["command"] == host["command"] == "python3 emit.py"
    assert contained["declared_head"] == host["declared_head"] == "python3"
    assert contained["returncode"] == host["returncode"] == 0
    assert METRIC in contained["diagnostic_tail"]
    assert METRIC in host["diagnostic_tail"]
