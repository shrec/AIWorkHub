"""NF-2026-01794: an xdist collection divergence is re-measured once, sequentially.

Measured three times on the ``Z:\\union2_*`` worktree mount: one pytest-xdist
worker failed to ``lstat`` a tracked, unchanged test file and the run ended
with ``Different tests were collected between gwN and gwM``; the same bytes
pass in a scratch copy. ``run_declared_validations`` now replays ONLY those
failed pytest commands once with ``-n 0`` and takes the verdict from the
replay. A failure without the signature, a non-pytest command, a command that
already pins ``-n`` and an environment-blocked failure are never replayed, and
the replay is never replayed.
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from aiworkhub import process_launcher_validation as validation
from aiworkhub import validation_runner
from aiworkhub.worker_workspace import (
    ValidationEnvironmentBlocked,
    ValidationRunError,
    WorkspaceError,
)

_PYTEST = "python -m pytest -q tests/test_a.py -p no:cacheprovider"
_RUFF = "python -m ruff check src/aiworkhub/a.py"
_DIVERGENCE = "\n".join(
    (
        "created: 8/8 workers",
        "8 workers [0 items]",
        "",
        "collecting ... ",
        "Different tests were collected between gw0 and gw3. The difference is:",
        "--- gw0",
        "+++ gw3",
        "=========================== no tests ran in 2.41s ===========================",
    )
)
_FAILED_ASSERT = "\n".join(
    (
        "F.                                                                [100%]",
        "E   AssertionError: assert 1 == 2",
        "=========================== short test summary info ============================",
        "FAILED tests/test_a.py::test_one - AssertionError: assert 1 == 2",
        "1 failed, 1 passed in 0.10s",
    )
)


def _row(command: str, returncode: int, stdout: str = "", **extra) -> dict:
    return {
        "command": command,
        "declared_command": command,
        "returncode": returncode,
        "timed_out": False,
        "stdout_tail": stdout,
        "stderr_tail": "",
        **extra,
    }


def _not_found(path_repr: str) -> str:
    return "\n".join(
        (
            "[gw2] win32 -- Python 3.12.7",
            "INTERNALERROR>   File \"_pytest/main.py\", line 283, in wrap_session",
            "INTERNALERROR> FileNotFoundError: [WinError 3] The system cannot find "
            f"the path specified: {path_repr}",
        )
    )


def _workspace(tmp_path):
    root = tmp_path / "worktree"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_a.py").write_text("def test_one():\n    pass\n", "utf-8")
    return SimpleNamespace(request_id="req-nf01794", path=root, repo=tmp_path / "repo")


def _exists(*present: str):
    return lambda recorded: recorded in present


# ---- the pure signature helper ---------------------------------------------


def test_divergence_literal_is_named() -> None:
    row = _row(_PYTEST, 1, _DIVERGENCE)
    assert (
        validation_runner.xdist_collection_replay_signature(
            _PYTEST, row, path_exists=_exists()
        )
        == validation_runner.XDIST_COLLECTION_DIVERGENCE
    )


def test_worker_file_not_found_needs_an_existing_path_and_a_worker() -> None:
    present = "Z:\\union2_ab\\tests\\test_a.py"
    stdout = _not_found("'Z:\\\\union2_ab\\\\tests\\\\test_a.py'")
    seen: list[str] = []

    def exists(recorded: str) -> bool:
        seen.append(recorded)
        return recorded == present

    assert (
        validation_runner.xdist_collection_replay_signature(
            _PYTEST, _row(_PYTEST, 3, stdout), path_exists=exists
        )
        == validation_runner.XDIST_WORKER_FILE_NOT_FOUND
    )
    # ``str(OSError)`` doubles a Windows backslash; the lookup sees it whole.
    assert seen == [present]
    # A path that really is missing is a candidate failure, never a flake.
    assert (
        validation_runner.xdist_collection_replay_signature(
            _PYTEST, _row(_PYTEST, 3, stdout), path_exists=_exists()
        )
        is None
    )
    # No xdist worker named anywhere: not an xdist collection failure.
    no_worker = stdout.replace("[gw2] ", "")
    assert (
        validation_runner.xdist_collection_replay_signature(
            _PYTEST, _row(_PYTEST, 3, no_worker), path_exists=exists
        )
        is None
    )


def test_path_lookup_fault_fails_closed() -> None:
    def broken(_recorded: str) -> bool:
        raise OSError("lookup failed")

    stdout = _not_found("'tests/test_a.py'")
    assert (
        validation_runner.xdist_collection_replay_signature(
            _PYTEST, _row(_PYTEST, 3, stdout), path_exists=broken
        )
        is None
    )


@pytest.mark.parametrize(
    "command",
    [
        _RUFF,
        "python -m pytest -q tests/test_a.py -n 0",
        "python -m pytest -q tests/test_a.py -n4",
        "python -m pytest -q tests/test_a.py --numprocesses=2",
        "python -m pytest -q tests/test_a.py -p no:xdist",
        "cd sub && python -m pytest -q tests/test_a.py",
    ],
)
def test_commands_that_cannot_be_replayed_are_never_named(command: str) -> None:
    assert (
        validation_runner.xdist_collection_replay_signature(
            command, _row(command, 1, _DIVERGENCE), path_exists=_exists()
        )
        is None
    )


@pytest.mark.parametrize(
    "row",
    [
        _row(_PYTEST, 0, _DIVERGENCE),
        _row(_PYTEST, 1, _DIVERGENCE, timed_out=True),
        _row(_PYTEST, None, _DIVERGENCE),
        _row(_PYTEST, 1, _FAILED_ASSERT),
        _row(_PYTEST, 1, _DIVERGENCE, xdist_collection_replay={"attempt": 1}),
    ],
)
def test_rows_without_a_replayable_failure_are_never_named(row: dict) -> None:
    assert (
        validation_runner.xdist_collection_replay_signature(
            _PYTEST, row, path_exists=_exists()
        )
        is None
    )


def test_replay_command_appends_sequential_xdist() -> None:
    assert (
        validation_runner.xdist_sequential_replay_command(_PYTEST + "  ")
        == _PYTEST + " -n 0"
    )


# ---- run_declared_validations ----------------------------------------------


class _Runner:
    """A fake ``run_validations``: one scripted outcome per call."""

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[list[str]] = []

    def __call__(self, _workspace, commands, **_route):
        self.calls.append(list(commands))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _run(workspace, commands, runner, baseline_comparer=None):
    return validation.run_declared_validations(
        workspace,
        {"validation": list(commands)},
        {},
        run_validations=runner,
        route_resolver=lambda _metadata: {"backend": "landlock"},
        baseline_comparer=baseline_comparer
        or (lambda *_args: pytest.fail("a passing replay must not reach baseline")),
    )


def _refuse(_workspace, _authority, _metadata, rows):
    raise ValidationRunError("validation_failed", rows)


def _tail_sha256(row: dict) -> str:
    payload = json.dumps([row["stdout_tail"], row["stderr_tail"]], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_divergence_replay_that_passes_returns_passing_rows(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    original = _row(_PYTEST, 1, _DIVERGENCE)
    replay_command = _PYTEST + " -n 0"
    runner = _Runner(
        ValidationRunError("validation_failed", [original, _row(_RUFF, 0)]),
        [_row(replay_command, 0, "2 passed in 0.20s")],
    )

    rows = _run(workspace, [_PYTEST, _RUFF], runner)

    assert runner.calls == [[_PYTEST, _RUFF], [replay_command]]
    assert [row["returncode"] for row in rows] == [0, 0]
    assert rows[0]["declared_command"] == _PYTEST
    assert rows[0]["command"] == _PYTEST
    assert rows[0]["xdist_collection_replay"] == {
        "attempt": 1,
        "signature": validation_runner.XDIST_COLLECTION_DIVERGENCE,
        "original_returncode": 1,
        "original_tail_sha256": _tail_sha256(original),
        "replay_command": replay_command,
    }
    assert "xdist_collection_replay" not in rows[1]
    assert all("behavioral_role" in row for row in rows)


def test_worker_file_not_found_in_the_workspace_is_replayed(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    recorded = str(workspace.path / "tests" / "test_a.py")
    stdout = _not_found(repr(recorded))
    runner = _Runner(
        ValidationRunError("validation_failed", [_row(_PYTEST, 3, stdout)]),
        [_row(_PYTEST + " -n 0", 0, "1 passed")],
    )

    rows = _run(workspace, [_PYTEST], runner)

    assert len(runner.calls) == 2
    assert (
        rows[0]["xdist_collection_replay"]["signature"]
        == validation_runner.XDIST_WORKER_FILE_NOT_FOUND
    )


def test_file_not_found_outside_the_workspace_is_not_replayed(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    outside = tmp_path / "elsewhere.py"
    outside.write_text("", "utf-8")
    stdout = _not_found(repr(str(outside)))
    runner = _Runner(ValidationRunError("validation_failed", [_row(_PYTEST, 3, stdout)]))

    with pytest.raises(ValidationRunError):
        _run(workspace, [_PYTEST], runner, _refuse)
    assert len(runner.calls) == 1


def test_divergence_replay_that_fails_keeps_the_failure_with_evidence(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    replay_command = _PYTEST + " -n 0"
    # The replay fails with the SAME signature: it is still never replayed again.
    runner = _Runner(
        ValidationRunError("validation_failed", [_row(_PYTEST, 1, _DIVERGENCE)]),
        ValidationRunError("validation_failed", [_row(replay_command, 1, _DIVERGENCE)]),
    )
    seen: list[list[dict]] = []

    def baseline(workspace_, authority, metadata, rows):
        seen.append(rows)
        return _refuse(workspace_, authority, metadata, rows)

    with pytest.raises(ValidationRunError) as caught:
        _run(workspace, [_PYTEST], runner, baseline)

    assert runner.calls == [[_PYTEST], [replay_command]]
    assert len(seen) == 1
    (row,) = caught.value.results
    assert row["returncode"] == 1
    assert row["declared_command"] == _PYTEST
    assert row["xdist_collection_replay"]["attempt"] == 1
    assert row["xdist_collection_replay"]["replay_command"] == replay_command
    assert row["xdist_collection_replay"]["original_returncode"] == 1


def test_replay_passing_beside_a_real_failure_is_still_a_failure(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    other = "python -m pytest -q tests/test_b.py"
    runner = _Runner(
        ValidationRunError(
            "validation_failed",
            [_row(_PYTEST, 1, _DIVERGENCE), _row(other, 1, _FAILED_ASSERT)],
        ),
        [_row(_PYTEST + " -n 0", 0, "1 passed")],
    )

    with pytest.raises(ValidationRunError) as caught:
        _run(workspace, [_PYTEST, other], runner, _refuse)

    assert runner.calls[1] == [_PYTEST + " -n 0"]
    first, second = caught.value.results
    assert first["returncode"] == 0 and "xdist_collection_replay" in first
    assert second["returncode"] == 1 and "xdist_collection_replay" not in second


@pytest.mark.parametrize(
    ("command", "stdout"),
    [
        (_PYTEST, _FAILED_ASSERT),
        (_RUFF, _DIVERGENCE),
        ("python -m pytest -q tests/test_a.py -n 2", _DIVERGENCE),
    ],
)
def test_unreplayable_failures_are_never_replayed(tmp_path, command, stdout) -> None:
    workspace = _workspace(tmp_path)
    runner = _Runner(ValidationRunError("validation_failed", [_row(command, 1, stdout)]))

    with pytest.raises(ValidationRunError) as caught:
        _run(workspace, [command], runner, _refuse)

    assert len(runner.calls) == 1
    assert "xdist_collection_replay" not in caught.value.results[0]


def test_environment_blocked_failure_is_not_an_xdist_replay(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    runner = _Runner(
        ValidationEnvironmentBlocked(
            "validation_environment_blocked:forbidden_spawn",
            [_row(_PYTEST, 1, _DIVERGENCE)],
            restriction="forbidden_spawn",
        )
    )

    # The capability planner owns this failure: it re-raises it, or refuses a
    # replay whose identity does not match -- never an xdist replay.
    with pytest.raises(ValidationRunError) as caught:
        _run(workspace, [_PYTEST], runner, _refuse)

    assert len(runner.calls) == 1
    assert "xdist_collection_replay" not in caught.value.results[0]


def test_replay_infrastructure_error_keeps_the_measured_failure(tmp_path) -> None:
    workspace = _workspace(tmp_path)
    runner = _Runner(
        ValidationRunError("validation_failed", [_row(_PYTEST, 1, _DIVERGENCE)]),
        WorkspaceError("validation_exec_scratch_unavailable"),
    )

    with pytest.raises(ValidationRunError, match="xdist_collection_replay_failed") as caught:
        _run(workspace, [_PYTEST], runner, _refuse)

    assert len(runner.calls) == 2
    assert caught.value.results[0]["returncode"] == 1
    assert "xdist_collection_replay" not in caught.value.results[0]
