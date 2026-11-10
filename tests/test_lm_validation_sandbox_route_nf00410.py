"""NF-2026-00410: VS Code LM validation runs under the host OS sandbox.

The worker's launch route stays in-process, but the declared validation commands
are candidate-authored code, so the validation route must name the real host
sandbox. A host with no usable sandbox fails closed by name, and only when the
card actually has commands to run.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import process_launcher, worker_workspace  # noqa: E402
from aiworkhub import process_launcher_validation as plv  # noqa: E402

_LM_ADAPTERS = sorted(process_launcher._VSCODE_LM_IN_PROCESS_ADAPTERS)
_IN_PROCESS = worker_workspace.VSCODE_LM_IN_PROCESS_BACKEND
_APPCONTAINER = worker_workspace.WINDOWS_APPCONTAINER_BACKEND
_REAL_BACKENDS = (_APPCONTAINER, "landlock", "bubblewrap")
_REFUSAL = "windows_appcontainer_sandbox_unavailable:test_host_has_no_sandbox"
_MARKER = "validation_command_ran.txt"
_COMMAND = f"python -c \"open('{_MARKER}', 'w').close()\""


def _host_sandbox_is(monkeypatch: pytest.MonkeyPatch, backend: str) -> None:
    monkeypatch.setattr(worker_workspace, "select_sandbox_backend", lambda: backend)


def _host_sandbox_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse() -> str:
        raise worker_workspace.WorkspaceError(_REFUSAL)

    monkeypatch.setattr(worker_workspace, "select_sandbox_backend", refuse)


def _host_sandbox_must_not_be_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail() -> str:
        pytest.fail("select_sandbox_backend must not be called on this path")

    monkeypatch.setattr(worker_workspace, "select_sandbox_backend", fail)


def _process_starts(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Record and refuse every process start; ``subprocess.run`` goes through it."""
    starts: list[Any] = []

    def refuse_start(*args: Any, **_kwargs: Any) -> None:
        starts.append(args)
        raise AssertionError("no validation process may start")

    monkeypatch.setattr(subprocess, "Popen", refuse_start)
    return starts


def _workspace(tmp_path: Path) -> worker_workspace.WorkerWorkspace:
    root = tmp_path / "request"
    (root / "worktree").mkdir(parents=True)
    (root / "home").mkdir()
    return worker_workspace.WorkerWorkspace(
        request_id="nf00410_request",
        repo=tmp_path,
        path=root / "worktree",
        home=root / "home",
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )


def _replay_metadata(adapter_id: str, recorded: str) -> dict[str, Any]:
    return {
        "adapter_id": adapter_id,
        "sandbox_backend": recorded,
        "execution_mode": "validation_only_replay",
        "provider_launched": False,
    }


@pytest.mark.parametrize("adapter_id", _LM_ADAPTERS)
@pytest.mark.parametrize("backend", _REAL_BACKENDS)
def test_lm_route_names_the_real_host_sandbox(
    monkeypatch: pytest.MonkeyPatch, adapter_id: str, backend: str
) -> None:
    _host_sandbox_is(monkeypatch, backend)

    route = process_launcher._validation_route_kwargs(
        {"adapter_id": adapter_id, "sandbox_backend": _IN_PROCESS}
    )

    assert route == {"backend": backend, "adapter_id": adapter_id}


@pytest.mark.parametrize("adapter_id", _LM_ADAPTERS)
def test_lm_route_without_a_launch_record_also_names_the_real_host_sandbox(
    monkeypatch: pytest.MonkeyPatch, adapter_id: str
) -> None:
    """Finding reproduction and base triage resolve the route from the adapter alone."""
    _host_sandbox_is(monkeypatch, _APPCONTAINER)

    assert process_launcher._validation_route_kwargs({"adapter_id": adapter_id}) == {
        "backend": _APPCONTAINER,
        "adapter_id": adapter_id,
    }


@pytest.mark.parametrize("adapter_id", _LM_ADAPTERS)
@pytest.mark.parametrize("recorded", ["deterministic_validation", _IN_PROCESS])
def test_validation_only_replay_names_the_real_host_sandbox(
    monkeypatch: pytest.MonkeyPatch, adapter_id: str, recorded: str
) -> None:
    _host_sandbox_is(monkeypatch, _APPCONTAINER)

    route = process_launcher._validation_route_kwargs(
        _replay_metadata(adapter_id, recorded)
    )

    assert route == {
        "backend": _APPCONTAINER,
        "adapter_id": adapter_id,
        "outer_validation_authority": True,
    }


@pytest.mark.parametrize("adapter_id", _LM_ADAPTERS)
def test_refused_sandbox_routes_no_backend_and_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, adapter_id: str
) -> None:
    _host_sandbox_refuses(monkeypatch)

    assert process_launcher._validation_route_kwargs(
        {"adapter_id": adapter_id, "sandbox_backend": _IN_PROCESS}
    ) == {"backend": None, "adapter_id": adapter_id}
    assert process_launcher._validation_route_kwargs(
        _replay_metadata(adapter_id, "deterministic_validation")
    ) == {
        "backend": None,
        "adapter_id": adapter_id,
        "outer_validation_authority": True,
    }


@pytest.mark.parametrize("adapter_id", _LM_ADAPTERS)
def test_refused_sandbox_fails_closed_by_name_and_starts_no_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, adapter_id: str
) -> None:
    _host_sandbox_refuses(monkeypatch)
    route = process_launcher._validation_route_kwargs(
        {"adapter_id": adapter_id, "sandbox_backend": _IN_PROCESS}
    )
    workspace = _workspace(tmp_path)
    starts = _process_starts(monkeypatch)

    with pytest.raises(worker_workspace.WorkspaceError) as refused:
        worker_workspace.run_validations(workspace, [_COMMAND], **route)

    expected = worker_workspace._validation_unsupported_in_sandbox_error(_REFUSAL)
    assert str(refused.value) == str(expected)
    assert starts == []
    assert not (workspace.path / _MARKER).exists()


def test_no_validation_commands_ask_for_no_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _host_sandbox_must_not_be_asked(monkeypatch)

    assert (
        worker_workspace.run_validations(
            _workspace(tmp_path), [], backend=None, adapter_id=_LM_ADAPTERS[0]
        )
        == []
    )


@pytest.mark.parametrize("adapter_id", _LM_ADAPTERS)
def test_lm_card_with_no_commands_finishes_on_a_host_with_no_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, adapter_id: str
) -> None:
    _host_sandbox_refuses(monkeypatch)
    route = process_launcher._validation_route_kwargs(
        {"adapter_id": adapter_id, "sandbox_backend": _IN_PROCESS}
    )

    assert worker_workspace.run_validations(_workspace(tmp_path), [], **route) == []


@pytest.mark.parametrize("adapter_id", _LM_ADAPTERS)
@pytest.mark.parametrize("replay", [False, True])
def test_manager_host_lane_keeps_the_host_backend_when_a_sandbox_exists(
    monkeypatch: pytest.MonkeyPatch, adapter_id: str, replay: bool
) -> None:
    _host_sandbox_is(monkeypatch, _APPCONTAINER)
    metadata: dict[str, Any] = {
        "adapter_id": adapter_id,
        "sandbox_backend": _IN_PROCESS,
        "validation_lane": plv.MANAGER_HOST_VALIDATION_LANE,
    }
    expected: dict[str, Any] = {
        "backend": plv.HOST_VALIDATION_LANE_BACKEND,
        "adapter_id": adapter_id,
    }
    if replay:
        metadata["execution_mode"] = "validation_only_replay"
        expected["outer_validation_authority"] = True

    assert process_launcher._validation_route_kwargs(metadata) == expected


@pytest.mark.parametrize("adapter_id", _LM_ADAPTERS)
@pytest.mark.parametrize("execution_mode", ["", "validation_only_replay"])
def test_recording_the_real_sandbox_as_the_lm_launch_is_still_drift(
    monkeypatch: pytest.MonkeyPatch, adapter_id: str, execution_mode: str
) -> None:
    """Drift is measured against the launch-bound backend, not the validation one."""
    _host_sandbox_is(monkeypatch, _APPCONTAINER)

    with pytest.raises(
        worker_workspace.WorkspaceError, match="validation_route_backend_mismatch"
    ):
        process_launcher._validation_route_kwargs(
            {
                "adapter_id": adapter_id,
                "sandbox_backend": _APPCONTAINER,
                "execution_mode": execution_mode,
            }
        )


@pytest.mark.parametrize("adapter_id", _LM_ADAPTERS)
def test_deterministic_lane_outside_replay_is_still_refused(
    monkeypatch: pytest.MonkeyPatch, adapter_id: str
) -> None:
    _host_sandbox_is(monkeypatch, _APPCONTAINER)

    with pytest.raises(
        worker_workspace.WorkspaceError, match="validation_route_backend_mismatch"
    ):
        process_launcher._validation_route_kwargs(
            {
                "adapter_id": adapter_id,
                "sandbox_backend": "deterministic_validation",
                "execution_mode": "provider_worker",
            }
        )


@pytest.mark.parametrize("backend", _REAL_BACKENDS)
def test_non_lm_adapter_keeps_its_launch_bound_route(
    monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    _host_sandbox_must_not_be_asked(monkeypatch)
    monkeypatch.setattr(process_launcher, "select_sandbox_backend", lambda: backend)

    assert process_launcher._validation_route_kwargs(
        {"adapter_id": "claude_cli", "sandbox_backend": backend}
    ) == {"backend": backend, "adapter_id": "claude_cli"}


def test_non_lm_adapter_still_raises_when_its_sandbox_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse() -> str:
        raise worker_workspace.WorkspaceError(_REFUSAL)

    monkeypatch.setattr(process_launcher, "select_sandbox_backend", refuse)

    with pytest.raises(worker_workspace.WorkspaceError, match=_REFUSAL):
        process_launcher._validation_route_kwargs({"adapter_id": "claude_cli"})
