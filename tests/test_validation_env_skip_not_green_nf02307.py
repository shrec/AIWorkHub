"""NF-2026-02307: a pytest row green only because sandbox tests were skipped is not green."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aiworkhub import validation_runner, validation_skip_audit as audit
from aiworkhub import worker_workspace as ww
from aiworkhub.terminal_failure_classification import validation_environment_disposition

ENV_SKIP_OUTPUT = """\
..s.
=========================== short test summary info ===========================
SKIPPED [2] tests/test_sandbox.py:12: AppContainer lane cannot nest the sandbox
SKIPPED [1] tests/test_host.py:7: Requires HOST execution
SKIPPED [1] tests/test_lane.py: not available in the lane
3 passed, 4 skipped in 0.10s
"""

ORDINARY_SKIP_OUTPUT = """\
..s
=========================== short test summary info ===========================
SKIPPED [1] tests/test_posix.py:5: unsupported on win32 (os.name)
2 passed, 1 skipped in 0.05s
"""


def _green_row(stdout: str) -> dict:
    return {
        "command": "python -m pytest -q tests/test_x.py",
        "declared_command": "python -m pytest -q tests/test_x.py",
        "returncode": 0,
        "sandbox_backend": "windows_appcontainer",
        "stdout_tail": stdout,
        **validation_runner.pytest_validation_evidence(stdout, 0),
    }


def test_environment_skips_are_listed_with_reasons() -> None:
    skips = audit.environment_skips(ENV_SKIP_OUTPUT)
    assert [item["location"] for item in skips] == [
        "tests/test_sandbox.py:12",
        "tests/test_host.py:7",
        "tests/test_lane.py",
    ]
    assert skips[0]["count"] == 2
    assert "AppContainer" in skips[0]["reason"]


def test_windows_drive_path_location_is_parsed() -> None:
    skips = audit.environment_skips(
        "SKIPPED [1] C:\\work\\tests\\test_a.py:9: needs nested sandbox support\n"
    )
    assert skips == [
        {
            "location": "C:\\work\\tests\\test_a.py:9",
            "count": 1,
            "reason": "needs nested sandbox support",
        }
    ]


@pytest.mark.parametrize("marker", audit.ENVIRONMENT_SKIP_MARKERS)
def test_every_marker_is_case_insensitive(marker: str) -> None:
    assert audit.environment_skips(f"SKIPPED [1] t.py:1: Why {marker.upper()} here\n")


def test_bare_sandbox_word_is_not_an_environment_marker() -> None:
    assert audit.environment_skips("SKIPPED [1] t.py:1: tests the sandbox module\n") == []


def test_ordinary_skip_is_not_an_environment_skip() -> None:
    assert audit.environment_skips(ORDINARY_SKIP_OUTPUT) == []
    assert audit.environment_skips("") == []
    assert audit.environment_skips("3 passed in 0.1s\n") == []


def test_ansi_coloured_skip_lines_are_parsed() -> None:
    line = "\x1b[33mSKIPPED [1] t.py:1: needs AppContainer\x1b[0m\n"
    assert [s["location"] for s in audit.environment_skips(line)] == ["t.py:1"]


def test_parser_is_linear_on_pathological_lines() -> None:
    hostile = "SKIPPED [1] " + "a:1" * 200_000 + "\n"
    assert audit.environment_skips(hostile) == []


def test_unaccounted_skips_fail_closed_as_unreadable() -> None:
    skips = audit.environment_skips("1 skipped in 0.01s\n", expected_skipped=1)
    assert [s["reason"] for s in skips] == [audit.SKIP_REASONS_UNREADABLE]
    # Fully accounted ordinary skips stay green.
    assert audit.environment_skips(ORDINARY_SKIP_OUTPUT, expected_skipped=1) == []


def test_env_skip_row_is_not_passed_and_environmental() -> None:
    row = audit.apply_env_skip_audit(_green_row(ENV_SKIP_OUTPUT), ENV_SKIP_OUTPUT)
    assert row["returncode"] is None and row["pytest_returncode"] == 0
    assert row[audit.VALIDATION_ENV_SKIPPED] is True
    assert [item["location"] for item in row["env_skipped_tests"]] == [
        "tests/test_sandbox.py:12",
        "tests/test_host.py:7",
        "tests/test_lane.py",
    ]
    assert "tests/test_sandbox.py:12" in row["infrastructure"]
    # Not passed for every existing failing-row consumer ...
    assert validation_runner.classify_validation_results([row]).blocks_acceptance
    # ... and routed to the typed environmental disposition (manager, no rework).
    disposition = validation_environment_disposition(
        terminal_state="validation_failed",
        validations=[row],
        required_outputs=[],
        required_output_records=[],
        task_id="t-1",
    )
    assert disposition is not None
    assert disposition["validation_environment_recovery"]["host_execution"] is False


def test_ordinary_skip_row_stays_green() -> None:
    row = audit.apply_env_skip_audit(_green_row(ORDINARY_SKIP_OUTPUT), ORDINARY_SKIP_OUTPUT)
    assert row["returncode"] == 0 and audit.VALIDATION_ENV_SKIPPED not in row
    assert not validation_runner.classify_validation_results([row]).blocks_acceptance


def test_failing_row_is_never_rewritten() -> None:
    row = {**_green_row(ENV_SKIP_OUTPUT), "returncode": 1}
    assert audit.apply_env_skip_audit(row, ENV_SKIP_OUTPUT) == row


def test_rs_injected_once_and_only_for_pytest() -> None:
    assert audit.with_skip_report("-p no:cacheprovider") == "-p no:cacheprovider -rs"
    assert audit.with_skip_report("-p no:cacheprovider -rs") == "-p no:cacheprovider -rs"
    assert ww._is_pytest_validation_command(["python", "-m", "pytest", "-q"])
    for non_pytest in (["python", "-m", "ruff", "check", "."], ["git", "status"]):
        assert not ww._is_pytest_validation_command(non_pytest)


# ---------------------------------------------------------------------------
# The real ``run_validations`` lane.  Only the process launch is replaced by a
# fake pytest that honours ``PYTEST_ADDOPTS`` and a command-line ``-r<chars>``
# as pytest does (the command line is parsed last and wins), so the lane's own
# ``-rs`` injection and audit are what is exercised.
# ---------------------------------------------------------------------------

_SKIP_LINE = "SKIPPED [1] tests/test_skipped.py:4: {reason}"
_PYTEST = "python -m pytest -q -p no:cacheprovider tests/test_skipped.py"


def _fake_pytest(reason: str, launched: list[dict]):
    def _run(argv, *, env, **kwargs):
        addopts = env.get("PYTEST_ADDOPTS", "").split()
        report = [a for a in [*addopts, *argv] if a.startswith("-r") and len(a) > 2]
        launched.append({"argv": list(argv), "addopts": addopts})
        lines = ["s", ""]
        if report and report[-1] != "-rN":
            lines += ["=== short test summary info ===", _SKIP_LINE.format(reason=reason)]
        lines.append("1 skipped in 0.01s")
        return subprocess.CompletedProcess(argv, 0, "\n".join(lines) + "\n", "")

    return _run


def _run_lane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reason: str, command: str
) -> dict:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_skipped.py").write_text("def test_one(): pass\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    workspace = ww.WorkerWorkspace(
        request_id="req-nf02307",
        repo=root,
        path=root,
        home=home,
        allowed_writes=(),
        parent_baseline={},
        workspace_baseline={},
    )
    launched: list[dict] = []
    monkeypatch.setattr(ww, "run_watched", _fake_pytest(reason, launched))
    monkeypatch.setattr(ww, "resolve_validation_pythonpath", lambda *a, **k: "")
    monkeypatch.setattr(ww, "_approved_pythonpath_site", lambda component: Path(component))
    monkeypatch.setattr(ww, "resolve_trusted_pytest_runtime_root", lambda *a, **k: tmp_path)
    # The skipping file is candidate-changed, so the NF-2026-02307 withhold applies
    # regardless of any enclosing git repository around tmp_path.
    monkeypatch.setattr(ww, "changed_paths", lambda workspace, **_: ["tests/test_skipped.py"])
    try:
        rows = ww.run_validations(
            workspace, [command], backend=ww.VSCODE_LM_IN_PROCESS_BACKEND, adapter_id="vscode_lm"
        )
    except ww.ValidationRunError as exc:  # a not-passed row aborts the batch with its rows
        rows = exc.results
    assert len(rows) == 1 and len(launched) == 1
    assert "-rs" in launched[0]["addopts"]  # injected for the pytest command
    return rows[0]


def test_run_validations_env_skip_row_is_not_passed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    row = _run_lane(monkeypatch, tmp_path, "inside the sandbox lane", _PYTEST)
    assert row["returncode"] is None
    assert row["pytest_returncode"] == 0
    assert row["failure_class"] == "validation_environment"
    assert row[audit.VALIDATION_ENV_SKIPPED] is True
    assert [item["location"] for item in row["env_skipped_tests"]] == [
        "tests/test_skipped.py:4"
    ]


def test_run_validations_suppressed_summary_is_unreadable_not_green(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    row = _run_lane(
        monkeypatch, tmp_path, "inside the sandbox lane", _PYTEST.replace("-q", "-q -rN")
    )
    assert row["returncode"] is None
    assert row[audit.VALIDATION_ENV_SKIPPED] is True
    assert [item["reason"] for item in row["env_skipped_tests"]] == [
        audit.SKIP_REASONS_UNREADABLE
    ]


def test_run_validations_ordinary_skip_stays_green(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    row = _run_lane(monkeypatch, tmp_path, "unsupported on this os", _PYTEST)
    assert row["returncode"] == 0
    assert audit.VALIDATION_ENV_SKIPPED not in row
