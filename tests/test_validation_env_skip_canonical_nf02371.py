"""NF-2026-02371: an env-skip authored by an unchanged canonical test stays green.

NF-2026-02307 withholds a green pytest row whose tests were skipped for an
environment reason.  When every skipping file (and every ``conftest.py`` on its
path) is byte-identical to canonical, the candidate did not author the skip, so
the row stays green, records the skipped paths as ``known_unknown`` evidence and
routes its command to the accept host validation stage.
"""

from __future__ import annotations

from typing import Any

import pytest

from aiworkhub import host_validation_stage as hvs
from aiworkhub import validation_runner, validation_skip_audit as audit

CANONICAL_SKIP_OUTPUT = """\
.......s
=========================== short test summary info ===========================
SKIPPED [1] tests/test_sandbox_lane.py:41: AppContainer lane cannot nest the sandbox
39 passed, 1 skipped in 3.10s
"""

COMMAND = "python -m pytest -q -n 0 -p no:cacheprovider tests/test_sandbox_lane.py"


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


def _assert_withheld(row: dict[str, Any]) -> None:
    assert row["returncode"] is None
    assert row[audit.VALIDATION_ENV_SKIPPED] is True
    assert audit.ENV_SKIP_CANONICAL not in row
    assert audit.HOST_STAGE_COMMAND not in row


def test_canonical_unchanged_skip_stays_green_with_known_unknown() -> None:
    row = audit.apply_env_skip_audit(
        _green_row(CANONICAL_SKIP_OUTPUT),
        CANONICAL_SKIP_OUTPUT,
        changed_paths=lambda: ("src/aiworkhub/other.py", "tests/test_other.py"),
    )
    assert row["returncode"] == 0
    assert row.get("failure_class") is None
    assert audit.VALIDATION_ENV_SKIPPED not in row
    assert row[audit.ENV_SKIP_CANONICAL] == ["tests/test_sandbox_lane.py"]
    known = row["known_unknown"]
    assert [item["kind"] for item in known] == [audit.ENV_SKIP_CANONICAL]
    assert known[0]["paths"] == ["tests/test_sandbox_lane.py"]
    assert row[audit.HOST_STAGE_COMMAND] == COMMAND
    assert not validation_runner.classify_validation_results([row]).blocks_acceptance


def test_default_without_changed_paths_is_the_nf02307_withhold() -> None:
    row = audit.apply_env_skip_audit(_green_row(CANONICAL_SKIP_OUTPUT), CANONICAL_SKIP_OUTPUT)
    _assert_withheld(row)


def test_raising_changed_paths_callable_is_the_nf02307_withhold() -> None:
    def _boom() -> list[str]:
        raise RuntimeError("git delta unreadable")

    row = audit.apply_env_skip_audit(
        _green_row(CANONICAL_SKIP_OUTPUT), CANONICAL_SKIP_OUTPUT, changed_paths=_boom
    )
    _assert_withheld(row)
    assert row["failure_class"] == "validation_environment"


def test_changed_paths_callable_is_lazy_without_env_skips() -> None:
    calls: list[int] = []

    def _tracked() -> list[str]:
        calls.append(1)
        return []

    output = "3 passed in 0.1s\n"
    row = audit.apply_env_skip_audit(_green_row(output), output, changed_paths=_tracked)
    assert row["returncode"] == 0
    timed_out = _green_row(CANONICAL_SKIP_OUTPUT, timed_out=True)
    _assert_withheld(audit.apply_env_skip_audit(timed_out, CANONICAL_SKIP_OUTPUT, changed_paths=_tracked))
    assert calls == []


@pytest.mark.parametrize(
    "changed",
    [
        {"tests/test_sandbox_lane.py"},  # candidate modified/added the skipping file
        {"tests/conftest.py"},  # candidate modified a conftest on its path
        {"conftest.py"},  # root conftest
        {"tests\\test_sandbox_lane.py"},  # Windows separator still matches
    ],
)
def test_candidate_authored_skip_is_still_withheld(changed: set[str]) -> None:
    row = audit.apply_env_skip_audit(
        _green_row(CANONICAL_SKIP_OUTPUT), CANONICAL_SKIP_OUTPUT, changed_paths=lambda: changed
    )
    _assert_withheld(row)


def test_nested_conftest_on_path_withholds() -> None:
    output = (
        "SKIPPED [1] tests/lane/test_a.py:3: requires host\n"
        "1 passed, 1 skipped in 0.01s\n"
    )
    row = audit.apply_env_skip_audit(
        _green_row(output), output, changed_paths=lambda: {"tests/lane/conftest.py"}
    )
    _assert_withheld(row)
    sibling = audit.apply_env_skip_audit(
        _green_row(output), output, changed_paths=lambda: {"tests/other/conftest.py"}
    )
    assert sibling["returncode"] == 0
    assert sibling[audit.ENV_SKIP_CANONICAL] == ["tests/lane/test_a.py"]


def test_non_zero_exit_is_never_excused() -> None:
    row = _green_row(CANONICAL_SKIP_OUTPUT)
    row["returncode"] = 1
    out = audit.apply_env_skip_audit(row, CANONICAL_SKIP_OUTPUT, changed_paths=set)
    assert out["returncode"] == 1
    assert audit.ENV_SKIP_CANONICAL not in out


def test_timed_out_row_is_never_excused() -> None:
    row = _green_row(CANONICAL_SKIP_OUTPUT, timed_out=True)
    _assert_withheld(audit.apply_env_skip_audit(row, CANONICAL_SKIP_OUTPUT, changed_paths=set))


@pytest.mark.parametrize(
    "output",
    [
        # pytest reported a skip whose reason line was not printed
        "1 skipped in 0.01s\n",
        # absolute Windows location cannot be compared to repo-relative paths
        "SKIPPED [1] C:\\work\\tests\\test_a.py:9: needs nested sandbox support\n"
        "1 skipped in 0.01s\n",
        # parent traversal
        "SKIPPED [1] ../tests/test_a.py:9: requires host\n1 skipped in 0.01s\n",
        # not a python test file
        "SKIPPED [1] tests/data.txt:1: requires host\n1 skipped in 0.01s\n",
    ],
)
def test_unparseable_skip_path_is_never_excused(output: str) -> None:
    row = audit.apply_env_skip_audit(_green_row(output), output, changed_paths=set)
    _assert_withheld(row)


def test_skip_path_extraction() -> None:
    skips = audit.environment_skips(
        "SKIPPED [2] tests/test_a.py:12: AppContainer\n"
        "SKIPPED [1] tests\\sub\\test_b.py: requires host\n"
        "SKIPPED [1] tests/test_a.py:30: AppContainer\n"
    )
    assert audit.environment_skip_paths(skips) == ["tests/test_a.py", "tests/sub/test_b.py"]
    assert audit.conftest_chain("tests/sub/test_b.py") == [
        "conftest.py",
        "tests/conftest.py",
        "tests/sub/conftest.py",
    ]


def _canonical_row() -> dict[str, Any]:
    return audit.apply_env_skip_audit(
        _green_row(CANONICAL_SKIP_OUTPUT), CANONICAL_SKIP_OUTPUT, changed_paths=set
    )


def test_canonical_row_routes_its_command_to_host_stage() -> None:
    row = _canonical_row()
    assert hvs.env_skip_host_commands([row, _green_row("3 passed in 0.1s\n")], [COMMAND]) == [
        COMMAND.split()
    ]
    card = hvs.card_with_env_skip_host_commands({"task_id": "t", "validation": [COMMAND]}, [row])
    assert hvs.card_requests_host_stage(card)
    spec = hvs.parse_host_spec(card)
    assert spec is not None and spec.commands == (tuple(COMMAND.split()),)
    assert spec.env_skip_paths == ((tuple(COMMAND.split()), ("tests/test_sandbox_lane.py",)),)


def test_card_without_canonical_rows_is_unchanged() -> None:
    card = {"task_id": "t"}
    withheld = audit.apply_env_skip_audit(_green_row(CANONICAL_SKIP_OUTPUT), CANONICAL_SKIP_OUTPUT)
    assert hvs.card_with_env_skip_host_commands(card, [withheld]) is card
    assert not hvs.card_requests_host_stage(hvs.card_with_env_skip_host_commands(card, []))


def test_existing_host_commands_are_kept_and_deduplicated() -> None:
    row = _canonical_row()
    other = ["python", "-m", "pytest", "-q", "tests/test_other.py"]
    card = {"host_commands": [other, COMMAND.split()], "validation": [COMMAND]}
    merged = hvs.card_with_env_skip_host_commands(card, [row, row])
    assert merged["host_commands"] == [other, COMMAND.split()]


def test_unroutable_command_fails_closed_at_host_spec() -> None:
    row = _canonical_row()
    row[audit.HOST_STAGE_COMMAND] = "python -c 'x y'"
    card = hvs.card_with_env_skip_host_commands({"validation": ["python -c 'x y'"]}, [row])
    assert hvs.card_requests_host_stage(card)
    with pytest.raises(hvs.HostStageError):
        hvs.parse_host_spec(card)


@pytest.mark.parametrize("declared", [None, [], ["python -m pytest -q tests/test_other.py"]])
def test_undeclared_host_stage_command_fails_closed(declared: Any) -> None:
    row = _canonical_row()
    row[audit.HOST_STAGE_COMMAND] = "python -m pytest -q tests/test_evil.py"
    card = hvs.card_with_env_skip_host_commands({"validation": declared}, [row])
    assert hvs.card_requests_host_stage(card)  # never dropped: the host check stays required
    with pytest.raises(hvs.HostStageError):
        hvs.parse_host_spec(card)


def test_changed_rootdir_ini_file_withholds() -> None:
    row = audit.apply_env_skip_audit(
        _green_row(CANONICAL_SKIP_OUTPUT),
        CANONICAL_SKIP_OUTPUT,
        changed_paths=lambda: ("tests/pytest.ini",),
    )
    _assert_withheld(row)


def _routed_spec() -> hvs.HostStageSpec:
    card = hvs.card_with_env_skip_host_commands({"validation": [COMMAND]}, [_canonical_row()])
    spec = hvs.parse_host_spec(card)
    assert spec is not None
    return spec


def test_routed_command_still_skipping_on_host_fails_the_stage() -> None:
    spec = _routed_spec()
    argv = spec.commands[0]
    still_skipped = {"returncode": 0, "stdout_tail": CANONICAL_SKIP_OUTPUT, "stderr_tail": ""}
    assert hvs.host_command_failed(spec, argv, still_skipped)
    ran = {"returncode": 0, "stdout_tail": "40 passed in 3.10s\n", "stderr_tail": ""}
    assert not hvs.host_command_failed(spec, argv, ran)
    assert hvs.host_command_failed(spec, argv, {**ran, "returncode": 1})


def test_unrouted_host_command_keeps_returncode_judgment() -> None:
    spec = hvs.HostStageSpec(commands=(tuple(COMMAND.split()),), sweep_tests=(), sweep_processes=0)
    row = {"returncode": 0, "stdout_tail": CANONICAL_SKIP_OUTPUT, "stderr_tail": ""}
    assert not hvs.host_command_failed(spec, spec.commands[0], row)
    other = ("python", "-m", "pytest", "-q", "tests/test_other.py")
    assert not hvs.host_command_failed(_routed_spec(), other, row)


@pytest.mark.parametrize(
    "stdout",
    [
        # review finding: summary counts a skip but no -rs reason line was printed
        "39 passed, 1 skipped in 3.10s\n",
        # review finding: an unparseable host skip location is never excused
        "SKIPPED [1] /abs/tests/test_sandbox_lane.py:41: requires host\n"
        "39 passed, 1 skipped in 3.10s\n",
    ],
)
def test_routed_host_skip_that_cannot_be_read_fails_closed(stdout: str) -> None:
    spec = _routed_spec()
    row = {"returncode": 0, "stdout_tail": stdout, "stderr_tail": ""}
    assert hvs.host_command_failed(spec, spec.commands[0], row)


def test_routed_command_runs_with_skip_reasons_only() -> None:
    spec = _routed_spec()
    assert hvs.host_command_argv(spec, spec.commands[0]) == [*COMMAND.split(), audit.SKIP_REPORT_FLAG]
    other = ("python", "-m", "pytest", "-q", "tests/test_other.py")
    assert hvs.host_command_argv(spec, other) == list(other)


@pytest.mark.parametrize("existing", ["python -m pytest -q", ["python -m pytest -q"], [{"a": 1}]])
def test_malformed_existing_host_commands_are_refused_not_coerced(existing: Any) -> None:
    card = hvs.card_with_env_skip_host_commands(
        {"host_commands": existing, "validation": [COMMAND]}, [_canonical_row()]
    )
    with pytest.raises(hvs.HostStageError):
        hvs.parse_host_spec(card)
