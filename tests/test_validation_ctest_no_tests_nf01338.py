"""NF-2026-01338: a ctest row that found no tests must never read as a pass.

``ctest --preset ...`` exits 0 when its test set is empty unless the preset's
``execution.noTestsAction`` is set to ``error`` -- unlike pytest, which exits 5
on zero collected tests. ``classify_validation_results`` previously only
inspected ``returncode``/``timed_out`` to decide whether a row failed, so a
zero-test ctest run was recorded as a pass. These tests cover the new
structural detector (``validation_runner.ctest_no_tests_found``) and its
effect on ``classify_validation_results``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub import validation_runner, worker_workspace  # noqa: E402


def _ctest_row(argv0: str, stdout_tail: str = "", returncode: int = 0) -> dict:
    argv = [argv0, "--preset", "windows-clang"]
    return {
        "command": " ".join(argv),
        "executed_argv": argv,
        "argv": argv,
        "returncode": returncode,
        "timed_out": False,
        "stdout_tail": stdout_tail,
        "stdout_head": stdout_tail,
        "stderr_tail": "",
        "stderr_head": "",
    }


# ---------------------------------------------------------------------------
# validation_runner.ctest_no_tests_found
# ---------------------------------------------------------------------------


def test_ctest_no_tests_found_detects_bare_executable():
    row = _ctest_row("ctest", "Test project ...\nNo tests were found!!!\n")
    assert validation_runner.ctest_no_tests_found(row) is True


def test_ctest_no_tests_found_detects_exe_suffix():
    row = _ctest_row("ctest.exe", "No tests were found!!!\n")
    assert validation_runner.ctest_no_tests_found(row) is True


def test_ctest_no_tests_found_detects_full_path_basenames():
    for argv0 in (
        r"C:\Program Files\CMake\bin\ctest.exe",
        "/usr/local/bin/ctest",
    ):
        row = _ctest_row(argv0, "No tests were found!!!\n")
        assert validation_runner.ctest_no_tests_found(row) is True


def test_ctest_row_that_ran_tests_is_not_flagged():
    row = _ctest_row("ctest", "Test #1: unit_test .... Passed\n100% tests passed\n")
    assert validation_runner.ctest_no_tests_found(row) is False


def test_non_ctest_executable_with_the_phrase_is_never_flagged():
    row = _ctest_row("pytest", "No tests were found!!!\n")
    assert validation_runner.ctest_no_tests_found(row) is False


# ---------------------------------------------------------------------------
# effect on classify_validation_results
# ---------------------------------------------------------------------------


def test_ctest_no_tests_row_with_rc_zero_fails_the_batch():
    passing_pytest_row = {
        "command": "pytest -q",
        "executed_argv": ["pytest", "-q"],
        "returncode": 0,
        "timed_out": False,
        "stdout_tail": "3 passed in 0.10s\n",
        "stderr_tail": "",
    }
    no_tests_row = _ctest_row("ctest", "No tests were found!!!\n")
    terminal = validation_runner.classify_validation_results(
        [passing_pytest_row, no_tests_row]
    )
    assert terminal.state == validation_runner.VALIDATION_FAILED
    assert terminal.blocks_acceptance is True


def test_batch_with_only_a_ctest_no_tests_row_is_not_passing():
    terminal = validation_runner.classify_validation_results(
        [_ctest_row("ctest", "No tests were found!!!\n")]
    )
    assert terminal.state != validation_runner.VALIDATION_PASSED


def test_ctest_row_that_passed_stays_passed():
    terminal = validation_runner.classify_validation_results(
        [_ctest_row("ctest", "100% tests passed, 0 tests failed out of 3\n")]
    )
    assert terminal.state == validation_runner.VALIDATION_PASSED


def test_pytest_row_is_unaffected_by_ctest_detection():
    row = {
        "command": "pytest -q",
        "executed_argv": ["pytest", "-q"],
        "returncode": 5,
        "timed_out": False,
        "stdout_tail": "no tests ran in 0.01s\n",
        "stderr_tail": "",
    }
    assert validation_runner.ctest_no_tests_found(row) is False
    terminal = validation_runner.classify_validation_results([row])
    assert terminal.state == validation_runner.VALIDATION_FAILED


# ---------------------------------------------------------------------------
# end-to-end: the real worker_workspace.run_validations path
# ---------------------------------------------------------------------------


def test_run_validations_end_to_end_ctest_no_tests_is_validation_failed(tmp_path, monkeypatch):
    """Drives the real ``run_validations`` control flow (not a hand-built row)
    with a stubbed subprocess launch, so a genuine zero-test ctest exit-0 run
    terminates as ``validation_failed``, never a pass.
    """
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    home = tmp_path / "home"
    for path in (repo, worktree, home):
        path.mkdir()

    workspace = worker_workspace.WorkerWorkspace(
        request_id="nf01338-e2e",
        repo=repo,
        path=worktree,
        home=home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )

    real_run = worker_workspace.subprocess.run

    def _fake_run(argv, **kwargs):
        if Path(str(argv[0])).name.lower() not in ("ctest", "ctest.exe"):
            return real_run(argv, **kwargs)
        return worker_workspace.subprocess.CompletedProcess(
            argv, 0, stdout="Test project X\nNo tests were found!!!\n", stderr=""
        )

    monkeypatch.setattr(worker_workspace.subprocess, "run", _fake_run)

    with pytest.raises(worker_workspace.ValidationRunError) as excinfo:
        worker_workspace.run_validations(
            workspace,
            ["ctest --preset windows-clang"],
            backend=worker_workspace.VSCODE_LM_IN_PROCESS_BACKEND,
            adapter_id="vscode_lm",
        )

    assert excinfo.value.terminal_state == validation_runner.VALIDATION_FAILED
    assert "validation_failed" in str(excinfo.value)
