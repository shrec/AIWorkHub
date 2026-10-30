from __future__ import annotations

from pathlib import Path

import pytest

from aiworkhub import process_launcher_validation as plv
from aiworkhub import quality_evidence
from aiworkhub.worker_workspace import WorkerWorkspace, WorkspaceError

PYTEST_ARGV = [".venv/bin/python", "-m", "pytest", "-q", "tests"]


def _row(
    failed: list[str],
    *,
    errors: list[str] | None = None,
    passed: int = 3,
    role: str = "suite_vs_base",
    argv: list[str] | None = None,
    summary: str | None = None,
    returncode: int | None = None,
    **extra,
) -> dict:
    errors = errors or []
    argv = argv or PYTEST_ARGV
    lines = [f"FAILED {node} - assert 0" for node in failed]
    lines += [f"ERROR {node} - boom" for node in errors]
    if summary is None:
        parts = []
        if failed:
            parts.append(f"{len(failed)} failed")
        parts.append(f"{passed} passed")
        if errors:
            parts.append(f"{len(errors)} error" + ("s" if len(errors) > 1 else ""))
        summary = ", ".join(parts) + " in 0.50s"
    row = {
        "command": " ".join(argv),
        "declared_command": " ".join(argv),
        "argv": argv,
        "declared_argv": argv,
        "executed_argv": argv,
        "interpreter_authority": {"path": argv[0]},
        "sandbox_backend": "landlock",
        "execution_boundary": "os_sandbox",
        "cwd": None,
        "env_override": None,
        "timeout_seconds": 30,
        "returncode": (1 if (failed or errors) else 0)
        if returncode is None
        else returncode,
        "timed_out": False,
        "stdout_tail": "\n".join(lines + [summary]),
        "stderr_tail": "",
        "stdout_truncated": False,
        "stderr_truncated": False,
        "behavioral_role": role,
    }
    row.update(extra)
    return row


def _workspace(tmp_path: Path) -> WorkerWorkspace:
    path = tmp_path / "candidate"
    home = tmp_path / "home"
    path.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    return WorkerWorkspace(
        request_id="candidate",
        repo=tmp_path,
        path=path,
        home=home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
        base_oid="b" * 40,
    )


def _compare(tmp_path: Path, candidate: dict, baseline: dict):
    workspace = _workspace(tmp_path)
    return plv.compare_schema_mypy_baseline(
        workspace,
        {"allowed_writes": []},
        {"adapter_id": "claude_cli"},
        [candidate],
        create_workspace=lambda *_a, **_k: workspace,
        cleanup_workspace=lambda *_a, **_k: None,
        run_validations=lambda *_a, **_k: [baseline],
        route_resolver=lambda _metadata: {},
    )


def test_role_is_registered_but_not_required():
    assert quality_evidence.VALIDATION_ROLE_SUITE_VS_BASE == "suite_vs_base"
    assert "suite_vs_base" in quality_evidence.VALIDATION_ROLES
    for roles in quality_evidence._REQUIRED_BEHAVIORAL_ROLES.values():
        assert "suite_vs_base" not in roles


def test_subset_of_base_failures_passes_with_pre_existing(tmp_path):
    candidate = _row(["tests/a.py::test_a"])
    baseline = _row(["tests/a.py::test_a", "tests/b.py::test_b"])
    rows = _compare(tmp_path, candidate, baseline)
    comparison = rows[0]["baseline_comparison"]
    assert rows[0]["returncode"] == 1
    assert comparison["schema_id"] == "aiworkhub.baseline_comparison.v1"
    assert comparison["kind"] == "pytest_suite_vs_base"
    assert comparison["outcome"] == "baseline_no_new_failures"
    assert comparison["base_oid"] == "b" * 40
    assert comparison["pre_existing"] == ["tests/a.py::test_a"]
    assert comparison["new_failures"] == []
    assert comparison["fixed"] == ["tests/b.py::test_b"]
    assert comparison["pre_existing_count"] == 1
    assert comparison["fixed_count"] == 1
    assert comparison["new_count"] == 0


def test_errors_and_failures_both_count(tmp_path):
    candidate = _row(["tests/a.py::test_a"], errors=["tests/c.py"])
    baseline = _row(["tests/a.py::test_a"], errors=["tests/c.py"])
    rows = _compare(tmp_path, candidate, baseline)
    assert rows[0]["baseline_comparison"]["pre_existing_count"] == 2


def test_new_failure_is_rejected_and_recorded(tmp_path):
    candidate = _row(["tests/a.py::test_a", "tests/n.py::test_new"])
    baseline = _row(["tests/a.py::test_a"])
    with pytest.raises(WorkspaceError, match="baseline_pytest_new_failures"):
        _compare(tmp_path, candidate, baseline)
    comparison = candidate["baseline_comparison"]
    assert comparison["outcome"] == "baseline_new_failures"
    assert comparison["new_failures"] == ["tests/n.py::test_new"]


def test_base_that_passes_makes_every_failure_new(tmp_path):
    candidate = _row(["tests/a.py::test_a"])
    baseline = _row([], returncode=0, summary="3 passed in 0.1s")
    with pytest.raises(WorkspaceError, match="baseline_pytest_new_failures"):
        _compare(tmp_path, candidate, baseline)


def test_evidence_lists_are_capped(tmp_path):
    ids = [f"tests/a.py::test_{index:04d}" for index in range(250)]
    rows = _compare(tmp_path, _row(ids), _row(ids))
    comparison = rows[0]["baseline_comparison"]
    assert comparison["pre_existing_count"] == 250
    assert len(comparison["pre_existing"]) == 200


@pytest.mark.parametrize(
    "mutation",
    [
        {"timed_out": True},
        {"stdout_truncated": True},
        {"stderr_truncated": True},
        {"stdout_tail": "no recognizable output"},
        {"stdout_tail": "FAILED tests/a.py::test_a - x\n2 failed, 1 passed in 0.1s"},
        {"stdout_tail": "FAILED tests/a.py::test_a - x"},
    ],
)
def test_unparseable_candidate_is_unparsed(tmp_path, mutation):
    candidate = _row(["tests/a.py::test_a"])
    candidate.update(mutation)
    with pytest.raises(WorkspaceError, match="baseline_pytest_failures_unparsed"):
        _compare(tmp_path, candidate, _row(["tests/a.py::test_a"]))


def test_unparseable_base_is_unparsed(tmp_path):
    baseline = _row(["tests/a.py::test_a"], stdout_truncated=True)
    with pytest.raises(WorkspaceError, match="baseline_pytest_failures_unparsed"):
        _compare(tmp_path, _row(["tests/a.py::test_a"]), baseline)


def test_candidate_without_ids_is_unparsed(tmp_path):
    candidate = _row([], returncode=2, summary="1 passed in 0.1s")
    with pytest.raises(WorkspaceError, match="baseline_pytest_failures_unparsed"):
        _compare(tmp_path, candidate, _row(["tests/a.py::test_a"]))


def test_other_role_or_non_pytest_command_is_ineligible(tmp_path):
    base = _row(["tests/a.py::test_a"])
    with pytest.raises(WorkspaceError, match="baseline_comparison_ineligible"):
        _compare(tmp_path, _row(["tests/a.py::test_a"], role="regression"), base)
    with pytest.raises(WorkspaceError, match="baseline_comparison_ineligible"):
        _compare(
            tmp_path,
            _row(["tests/a.py::test_a"], argv=[".venv/bin/python", "check.py"]),
            base,
        )


def test_mypy_branch_evidence_keys_unchanged(tmp_path):
    argv = [".venv/bin/python", "-m", "mypy", "src"]
    candidate = _row([], role="schema", argv=argv, returncode=1)
    candidate["stdout_tail"] = "src/a.py:1: error: x  [arg-type]\nFound 1 error"
    baseline = dict(candidate)
    rows = _compare(tmp_path, candidate, baseline)
    comparison = rows[0]["baseline_comparison"]
    assert "kind" not in comparison
    assert comparison["outcome"] == "baseline_no_new_diagnostics"
