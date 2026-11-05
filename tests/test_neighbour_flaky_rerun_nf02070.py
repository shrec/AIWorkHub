"""A red neighbour node is re-run once, serially, before accept refuses (NF-2026-02070)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from aiworkhub import accept_neighbour_tests as neighbours
from aiworkhub.worker_workspace import ValidationRunError, WorkspaceError

_CARD = {"allowed_writes": ["src/aiworkhub/engine.py"], "validation": ["x"]}
_SELECTION = neighbours.NeighbourSelection(
    tests=("tests/test_b.py", "tests/test_a.py"), reason="", limit=12,
    production_paths=("src/aiworkhub/engine.py",), related_count=2,
)
_FLAKY = "tests/test_a.py::test_load_sensitive"


def _red(*nodes: str) -> dict[str, Any]:
    tail = "".join(f"FAILED {node} - AssertionError: slow\n" for node in nodes)
    return {"command": "c", "returncode": 1, "timed_out": False, "stdout_tail": tail}


_GREEN = {"command": "c", "returncode": 0, "timed_out": False, "stdout_tail": "1 passed"}


@pytest.fixture(autouse=True)
def _sixteen_cores(monkeypatch: pytest.MonkeyPatch) -> None:
    """NF-2026-02069: the neighbour width is explicit and core-derived; pin the cores."""
    monkeypatch.setattr(neighbours.os, "cpu_count", lambda: 16)


class _Runner:
    """Returns (or raises) one scripted outcome per call and records each command."""

    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.commands: list[str] = []

    def __call__(self, workspace: Any, authority: Any, route_metadata: Any) -> Any:
        [command] = authority["validation"]
        self.commands.append(command)
        outcome = self.outcomes[min(len(self.commands), len(self.outcomes)) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _clock() -> Any:
    ticks = iter([10.0, 12.5, 20.0, 21.25])
    return lambda: next(ticks)


def _run(runner: _Runner) -> dict[str, Any]:
    return neighbours.run_neighbour_tests(
        _SELECTION, _CARD, run=runner, workspace="union", route_metadata={},
        clock=_clock(),
    )


def _refusal(runner: _Runner) -> dict[str, Any]:
    with pytest.raises(WorkspaceError) as excinfo:
        _run(runner)
    message = str(excinfo.value)
    assert message.startswith(neighbours.NEIGHBOUR_FAILURE_PREFIX)
    return json.loads(message[len(neighbours.NEIGHBOUR_FAILURE_PREFIX):])


@pytest.mark.parametrize("raised", [False, True])
def test_a_node_that_passes_on_the_serial_rerun_is_recorded_not_refused(raised: bool) -> None:
    first: Any = [_red(_FLAKY)]
    if raised:
        first = ValidationRunError("validation_failed:x", first)
    runner = _Runner(first, [_GREEN])
    evidence = _run(runner)
    assert runner.commands == [
        "python -m pytest -n 2 tests/test_b.py tests/test_a.py -q",
        f"python -m pytest -n 0 {_FLAKY} -q",
    ]
    rerun = evidence["neighbour_flaky_rerun"]
    assert rerun["nodes"] == [_FLAKY]
    assert rerun["duration_seconds"] == 1.25
    assert rerun["validation"] == [_GREEN]
    assert evidence["duration_seconds"] == 2.5


def test_a_node_red_on_both_runs_refuses_naming_it() -> None:
    other = "tests/test_b.py::test_other"
    runner = _Runner([_red(_FLAKY, other)], [_red(_FLAKY)])
    detail = _refusal(runner)
    assert runner.commands[1] == f"python -m pytest -n 0 {_FLAKY} {other} -q"
    assert detail["failed"] == [_FLAKY]
    assert detail["tests"] == ["tests/test_b.py", "tests/test_a.py"]


def test_an_erroring_rerun_without_parseable_ids_names_every_rerun_node() -> None:
    runner = _Runner([_red(_FLAKY)], WorkspaceError("validation_environment_blocked:x"))
    detail = _refusal(runner)
    assert len(runner.commands) == 2
    assert detail["failed"] == [_FLAKY]
    assert detail["first_failure"] == "validation_environment_blocked:x"


def test_a_rerun_returning_no_rows_refuses() -> None:
    runner = _Runner([_red(_FLAKY)], [])
    assert _refusal(runner)["failed"] == [_FLAKY]
    assert len(runner.commands) == 2


@pytest.mark.parametrize(
    "outcome",
    [
        [],
        WorkspaceError("validation_environment_blocked:x"),
        [{"command": "c", "returncode": 1, "timed_out": False, "stdout_tail": "boom"}],
        [{"command": "c", "returncode": 0, "timed_out": True}],
        ValidationRunError("validation_failed:x", []),
    ],
    ids=["no_rows", "workspace_error", "red_no_ids", "timed_out_no_ids", "error_no_rows"],
)
def test_runs_that_are_not_a_parseable_red_verdict_refuse_without_a_rerun(
    outcome: Any,
) -> None:
    runner = _Runner(outcome, [_GREEN])
    _refusal(runner)
    assert len(runner.commands) == 1


def test_a_red_run_with_rows_on_a_non_verdict_error_is_not_rerun() -> None:
    class _Blocked(ValidationRunError):
        terminal_state = "validation_environment_blocked"

    runner = _Runner(_Blocked("blocked:x", [_red(_FLAKY)]), [_GREEN])
    assert _refusal(runner)["failed"] == [_FLAKY]
    assert len(runner.commands) == 1


def test_more_failures_than_the_cap_refuse_without_a_rerun() -> None:
    nodes = [
        f"tests/test_a.py::test_{index}"
        for index in range(neighbours.MAX_REPORTED_FAILURES + 1)
    ]
    runner = _Runner([_red(*nodes)], [_GREEN])
    detail = _refusal(runner)
    assert len(runner.commands) == 1
    assert len(detail["failed"]) == neighbours.MAX_REPORTED_FAILURES


def test_exactly_the_cap_is_rerun() -> None:
    nodes = [
        f"tests/test_a.py::test_{index}"
        for index in range(neighbours.MAX_REPORTED_FAILURES)
    ]
    runner = _Runner([_red(*nodes)], [_GREEN])
    assert _run(runner)["neighbour_flaky_rerun"]["nodes"] == nodes


@pytest.mark.parametrize(
    "node",
    ["tests/test_unselected.py::test_x", "tests/test_a.py::test_x'quote"],
)
def test_a_node_outside_the_selection_or_needing_quotes_is_not_rerun(node: str) -> None:
    runner = _Runner([_red(node)], [_GREEN])
    _refusal(runner)
    assert len(runner.commands) == 1


def test_the_serial_command_shares_the_neighbour_command_shape() -> None:
    assert neighbours.neighbour_command(["tests/test_a.py"]) == (
        "python -m pytest -n 1 tests/test_a.py -q"
    )
    assert neighbours.neighbour_command([_FLAKY], serial=True) == (
        f"python -m pytest -n 0 {_FLAKY} -q"
    )
