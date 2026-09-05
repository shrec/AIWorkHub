from __future__ import annotations

from types import SimpleNamespace

import pytest

from aiworkhub import process_launcher
from aiworkhub import process_launcher_validation as validation


@pytest.mark.parametrize("reason", [
    "metadata_broker_hardlink_forbidden", "metadata_broker_deleted_fd",
])
def test_structural_metadata_denial_does_not_repeat_identical_lane(
    tmp_path, reason
) -> None:
    workspace = SimpleNamespace(
        request_id="retained", path=tmp_path / "worktree", repo=tmp_path / "repo"
    )
    row = {
        "command": "python -m pytest tests/test_x.py", "returncode": 126,
        "metadata_broker_denial_attributed": True,
        "metadata_broker_denials": [{
            "schema": "aiworkhub.metadata_broker_denial.v1",
            "authenticated": True, "terminal": True,
            "reason": reason, "syscall_nr": 90,
        }],
    }
    calls = []

    def run(candidate, commands, **route):
        calls.append((candidate, commands, route))
        raise validation.ValidationEnvironmentBlocked(
            "validation_environment_blocked:metadata_broker_denial", [row],
            restriction="metadata_broker_denial",
        )

    with pytest.raises(
        validation.ValidationEnvironmentBlocked,
        match="compatible_validation_lane_unavailable:backend=landlock:capabilities=",
    ) as caught:
        validation.run_declared_validations(
            workspace, {"validation": [row["command"]]}, {},
            run_validations=run,
            route_resolver=lambda _: {"backend": "landlock"},
            baseline_comparer=lambda *args: pytest.fail("must retain denial"),
        )
    assert len(calls) == 1
    assert caught.value.results[0]["metadata_broker_denials"] == row["metadata_broker_denials"]
    assert caught.value.results[0]["returncode"] == 126
    assert "validation_capability_replay" not in caught.value.results[0]


def _mypy_row(*lines: str) -> dict:
    return {
        "declared_command": "python -m mypy src",
        "declared_argv": ["python", "-m", "mypy", "src"],
        "executed_argv": ["python", "-m", "mypy", "src"],
        "interpreter_authority": {"path": "python"},
        "sandbox_backend": "landlock",
        "execution_boundary": "os_sandbox",
        "cwd": None,
        "env_override": None,
        "timeout_seconds": 30,
        "returncode": 1,
        "timed_out": False,
        "stdout_tail": "\n".join((*lines, f"Found {len(lines)} errors")),
        "stderr_tail": "",
        "stdout_truncated": False,
        "stderr_truncated": False,
        "failure_receipt": {"failure_class": "type_check_failure"},
    }


def test_diagnostic_normalization_is_line_neutral_and_preserves_multiplicity() -> None:
    diagnostics = validation.schema_mypy_diagnostics(
        _mypy_row(
            "./src/a.py:2:4: error: stable   message  [arg-type]",
            "src/a.py:99: error: stable message  [arg-type]",
        )
    )

    assert diagnostics[("src/a.py", "arg-type", "stable message")] == 2


def test_diagnostic_normalization_fails_closed_on_unexpected_returncode() -> None:
    row = _mypy_row("src/a.py:1: error: x  [arg-type]")
    row["returncode"] = 2

    with pytest.raises(
        validation.WorkspaceError, match="baseline_mypy_candidate_not_comparable"
    ):
        validation.schema_mypy_diagnostics(row)


def test_launcher_private_diagnostic_helpers_are_compatible() -> None:
    row = _mypy_row("src/a.py:1: error: x  [arg-type]")

    assert process_launcher._exact_schema_mypy_invocation(row)
    assert process_launcher._schema_mypy_diagnostics(row) == (
        validation.schema_mypy_diagnostics(row)
    )
    assert process_launcher._baseline_validation_identity(row) == (
        validation.baseline_validation_identity(row)
    )


def test_route_boundary_uses_injected_authority_without_host_selection() -> None:
    calls: list[str] = []

    def backend(adapter_id: str) -> str:
        calls.append(adapter_id)
        return "landlock"

    assert validation.validation_route_kwargs(
        {"adapter_id": "claude_cli", "sandbox_backend": "landlock"}, backend
    ) == {"adapter_id": "claude_cli", "backend": "landlock"}
    assert calls == ["claude_cli"]


def test_route_boundary_rejects_recorded_authority_mismatch() -> None:
    with pytest.raises(
        validation.WorkspaceError, match="validation_route_backend_mismatch"
    ):
        validation.validation_route_kwargs(
            {"adapter_id": "claude_cli", "sandbox_backend": "bubblewrap"},
            lambda _adapter_id: "landlock",
        )
