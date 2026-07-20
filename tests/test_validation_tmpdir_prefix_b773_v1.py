from __future__ import annotations

import pytest

from geoai_task_mcp.worker_workspace import (
    WorkspaceError,
    _parse_validation_command_detailed,
    parse_validation_command,
)


def test_exact_tmpdir_prefix_preserves_public_api() -> None:
    command = "TMPDIR=/dev/shm bash bitnnv2/tests/smoke.sh"
    assert parse_validation_command(command) == (
        ["bash", "bitnnv2/tests/smoke.sh"],
        (),
    )
    assert _parse_validation_command_detailed(command) == (
        ["bash", "bitnnv2/tests/smoke.sh"],
        (),
        "/dev/shm",
    )


@pytest.mark.parametrize(
    "command",
    [
        "TMPDIR=/tmp bash test.sh",
        "TMPDIR=relative bash test.sh",
        "TMPDIR= bash test.sh",
        "PATH=/bin bash test.sh",
        "LD_PRELOAD=x bash test.sh",
        "TMPDIR=/dev/shm PYTHONPATH=. bash test.sh",
        "TMPDIR=/dev/shm; bash test.sh",
        "TMPDIR=/dev/shm$(id) bash test.sh",
        "TMPDIR=/dev/shm`id` bash test.sh",
    ],
)
def test_other_assignments_and_shell_syntax_remain_rejected(command: str) -> None:
    with pytest.raises(WorkspaceError):
        _parse_validation_command_detailed(command)


def test_plain_and_pythonpath_parsing_are_unchanged() -> None:
    assert parse_validation_command("python3 -m json.tool out.json") == (
        ["python3", "-m", "json.tool", "out.json"],
        (),
    )
    assert parse_validation_command("PYTHONPATH=. python3 -m pytest -q") == (
        ["python3", "-m", "pytest", "-q"],
        (".",),
    )
