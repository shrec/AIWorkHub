"""NF-2026-02374: an env-skip in a candidate-changed test file routes to the host stage.

NF-2026-02371 kept a green row whose env-skips came from canonical-unchanged
test files and routed its command to the accept-time host stage.  A skip in a
file the candidate itself changed was withheld, and its recovery plan was a
lane replay that repeats the same skip forever.  That row now routes the same
way, marked candidate-authored; a changed conftest/rootdir file, a truncated or
unparseable report and a timed-out row stay the NF-2026-02307 withhold, and a
withheld env-skip row no longer recommends a lane replay.
"""

from __future__ import annotations

from typing import Any

import pytest

from aiworkhub import host_validation_stage as hvs
from aiworkhub import validation_runner, validation_skip_audit as audit
from aiworkhub.terminal_failure_classification import validation_environment_disposition

SKIP_FILE = "tests/test_sandbox_lane.py"
SKIP_OUTPUT = f"""\
.......s
=========================== short test summary info ===========================
SKIPPED [1] {SKIP_FILE}:41: AppContainer lane cannot nest the sandbox
39 passed, 1 skipped in 3.10s
"""
COMMAND = f"python -m pytest -q -n 0 -p no:cacheprovider {SKIP_FILE}"


def _green_row(stdout: str, **extra: Any) -> dict[str, Any]:
    return {
        "command": COMMAND,
        "declared_command": COMMAND,
        "returncode": 0,
        "sandbox_backend": "windows_appcontainer",
        "stdout_tail": stdout,
        **validation_runner.pytest_validation_evidence(stdout, 0),
        **extra,
    }


def _audit(stdout: str = SKIP_OUTPUT, changed: Any = (SKIP_FILE,), **extra: Any) -> dict[str, Any]:
    return audit.apply_env_skip_audit(
        _green_row(stdout, **extra), stdout, changed_paths=lambda: changed
    )


def _assert_withheld(row: dict[str, Any]) -> None:
    assert row["returncode"] is None
    assert row[audit.VALIDATION_ENV_SKIPPED] is True
    assert audit.HOST_STAGE_COMMAND not in row
    assert audit.ENV_SKIP_CANONICAL not in row
    assert audit.ENV_SKIP_CANDIDATE_CHANGED not in row
    assert audit.ENV_SKIP_CANDIDATE_AUTHORED not in row


@pytest.mark.parametrize("changed", [(SKIP_FILE,), ("tests\\test_sandbox_lane.py",)])
def test_candidate_changed_skip_gets_routing_evidence(changed: tuple[str, ...]) -> None:
    row = _audit(changed=changed)
    assert row["returncode"] == 0
    assert row.get("failure_class") is None
    assert audit.VALIDATION_ENV_SKIPPED not in row
    assert audit.ENV_SKIP_CANONICAL not in row
    assert row[audit.ENV_SKIP_CANDIDATE_CHANGED] == [SKIP_FILE]
    assert row[audit.ENV_SKIP_CANDIDATE_AUTHORED] == [SKIP_FILE]
    assert row[audit.HOST_STAGE_COMMAND] == COMMAND
    assert [item["location"] for item in row["env_skipped_tests"]] == [f"{SKIP_FILE}:41"]
    known = row["known_unknown"]
    assert [item["kind"] for item in known] == ["validation_env_skip_candidate_changed"]
    assert known[0]["paths"] == [SKIP_FILE]
    assert known[0][audit.ENV_SKIP_CANDIDATE_AUTHORED] == [SKIP_FILE]


def test_marker_lists_only_the_changed_skipping_files() -> None:
    output = (
        "SKIPPED [1] tests/test_a.py:3: requires host\n"
        "SKIPPED [1] tests/test_b.py:4: requires host\n"
        "1 passed, 2 skipped in 0.01s\n"
    )
    row = _audit(output, changed=("tests/test_b.py",))
    assert row[audit.ENV_SKIP_CANDIDATE_CHANGED] == ["tests/test_a.py", "tests/test_b.py"]
    assert row[audit.ENV_SKIP_CANDIDATE_AUTHORED] == ["tests/test_b.py"]


def test_canonical_unchanged_output_is_unchanged() -> None:
    row = _audit(changed=("src/aiworkhub/other.py",))
    assert row[audit.ENV_SKIP_CANONICAL] == [SKIP_FILE]
    assert audit.ENV_SKIP_CANDIDATE_CHANGED not in row
    assert audit.ENV_SKIP_CANDIDATE_AUTHORED not in row
    assert [item["kind"] for item in row["known_unknown"]] == [audit.ENV_SKIP_CANONICAL]


def _routed_spec() -> hvs.HostStageSpec:
    card = hvs.card_with_env_skip_host_commands({"validation": [COMMAND]}, [_audit()])
    assert hvs.card_requests_host_stage(card)
    spec = hvs.parse_host_spec(card)
    assert spec is not None
    return spec


def test_candidate_changed_row_routes_into_host_commands() -> None:
    row = _audit()
    assert hvs.env_skip_host_commands([row], [COMMAND]) == [COMMAND.split()]
    card = hvs.card_with_env_skip_host_commands({"task_id": "t", "validation": [COMMAND]}, [row])
    assert card["host_commands"] == [COMMAND.split()]
    spec = _routed_spec()
    assert spec.commands == (tuple(COMMAND.split()),)
    assert spec.env_skip_paths == ((tuple(COMMAND.split()), (SKIP_FILE,)),)
    assert hvs.host_command_argv(spec, spec.commands[0]) == [*COMMAND.split(), audit.SKIP_REPORT_FLAG]


def test_undeclared_candidate_command_fails_closed() -> None:
    row = _audit()
    row[audit.HOST_STAGE_COMMAND] = "python -m pytest -q tests/test_evil.py"
    card = hvs.card_with_env_skip_host_commands({"validation": [COMMAND]}, [row])
    assert hvs.card_requests_host_stage(card)
    with pytest.raises(hvs.HostStageError):
        hvs.parse_host_spec(card)


def test_host_run_still_env_skipping_the_changed_file_fails() -> None:
    spec = _routed_spec()
    argv = spec.commands[0]
    still = {"returncode": 0, "stdout_tail": SKIP_OUTPUT, "stderr_tail": ""}
    assert hvs.host_command_failed(spec, argv, still)
    unreadable = {"returncode": 0, "stdout_tail": "39 passed, 1 skipped in 3.10s\n", "stderr_tail": ""}
    assert hvs.host_command_failed(spec, argv, unreadable)
    ran = {"returncode": 0, "stdout_tail": "40 passed in 3.10s\n", "stderr_tail": ""}
    assert not hvs.host_command_failed(spec, argv, ran)


@pytest.mark.parametrize(
    "changed",
    [
        (SKIP_FILE, "tests/conftest.py"),
        (SKIP_FILE, "conftest.py"),
        (SKIP_FILE, "pyproject.toml"),
        (SKIP_FILE, "tests/pytest.ini"),
    ],
)
def test_changed_conftest_or_rootdir_file_withholds(changed: tuple[str, ...]) -> None:
    _assert_withheld(_audit(changed=changed))


def test_truncated_skip_report_withholds() -> None:
    lines = "".join(
        f"SKIPPED [1] tests/test_m{index}.py:1: requires host\n"
        for index in range(audit.MAX_REPORTED_SKIPS)
    )
    output = f"{lines}{audit.MAX_REPORTED_SKIPS} skipped in 0.1s\n"
    _assert_withheld(_audit(output, changed=("tests/test_m0.py",)))


@pytest.mark.parametrize(
    "output",
    [
        "39 passed, 1 skipped in 3.10s\n",
        "SKIPPED [1] /abs/tests/test_sandbox_lane.py:41: requires host\n1 skipped in 0.01s\n",
    ],
)
def test_unreadable_or_unparseable_skip_withholds(output: str) -> None:
    _assert_withheld(_audit(output))


def test_timed_out_missing_or_raising_changed_paths_withhold() -> None:
    _assert_withheld(_audit(timed_out=True))
    _assert_withheld(audit.apply_env_skip_audit(_green_row(SKIP_OUTPUT), SKIP_OUTPUT))

    def _boom() -> list[str]:
        raise RuntimeError("git delta unreadable")

    _assert_withheld(
        audit.apply_env_skip_audit(_green_row(SKIP_OUTPUT), SKIP_OUTPUT, changed_paths=_boom)
    )


def test_withheld_env_skip_recovery_has_no_lane_replay_step() -> None:
    row = _audit(changed=(SKIP_FILE, "tests/conftest.py"))
    _assert_withheld(row)
    disposition = validation_environment_disposition(
        terminal_state="validation_failed",
        validations=[row, {"command": "ruff check src", "returncode": 0}],
        required_outputs=[],
        required_output_records=[],
        task_id="t-1",
    )
    assert disposition is not None
    recovery = disposition["validation_environment_recovery"]
    assert recovery["lane_replay_deterministic"] is True
    assert recovery["host_execution"] is False
    tools = [step["tool"] for step in recovery["plan"]]
    assert tools == ["aiworkhub_task_reject_review"]
    assert all(
        "validation_only_replay" not in step.get("arguments", {}) for step in recovery["plan"]
    )
