"""NF-2026-01671: a sandbox-signature validation failure is classified, never run.

A declared test that RAN in the worker sandbox and failed only on a measured
lane restriction (WinError 1314 symlink privilege, a ``_multiprocessing.SemLock``
denial) used to be a plain ``validation_failed`` the manager needed four turns
to classify and replay. The finalizer now attaches a typed
``validation_environment`` disposition plus a recovery plan for a MANAGER to
execute -- classification is automatic, execution is not, and nothing here
launches a provider or runs the candidate on the host.

The match fails closed everywhere else: an assertion that mentions the
signature, a second failure in the same row, a truncated row whose closing
summary block is out of view, a signature that only appears in prose, or a
missing required output.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from aiworkhub import process_launcher
from aiworkhub import process_launcher_validation as validation
from aiworkhub import terminal_failure_classification as tfc
from aiworkhub import validation_runner, worker_workspace
from aiworkhub.worker_workspace import (
    ValidationEnvironmentBlocked,
    ValidationRunError,
    WorkspaceError,
)

_COMMAND = "python -m pytest -q tests/test_links.py"
_OUTPUT = "tests/test_links.py"
_TASK_ID = "TASK_NF01671"
_RUNNER = "claude_worker_nf01671"
_DIGEST = "file:644:" + "ab" * 32
_SYMLINK = "windows_symlink_privilege_not_held"
_SEMLOCK = "multiprocessing_semlock_denied"
# The process_launcher module-size ratchet at this card's base: headroom 0.
_PROCESS_LAUNCHER_LINE_CEILING = 14148

_WINERROR = (
    "OSError: [WinError 1314] A required privilege is not held by the client: "
    "'link-src' -> 'link-dst'"
)
_TYPEERROR = "TypeError: unsupported operand type(s) for +: 'int' and 'str'"
_SEMLOCK_DENIED = "PermissionError: [Errno 13] Permission denied"
_SYMLINK_FRAMES = (
    "tests/test_links.py:12: in {name}",
    "    os.symlink('link-src', 'link-dst')",
)
_SEMLOCK_FRAMES = (
    "tests/test_links.py:30: in {name}",
    "    lock = multiprocessing.Lock()",
    "/usr/lib/python3.12/multiprocessing/synchronize.py:57: in __init__",
    "    sl = self._semlock = _multiprocessing.SemLock(",
)


def _pytest_stdout(failures, *, passed=3, filler=0, trailer=""):
    """``pytest -q --tb=short`` stdout for ``(name, exception, frames)`` failures."""
    lines = [
        "." * passed + "F" * len(failures) + "    [100%]",
        "=" * 30 + " FAILURES " + "=" * 30,
    ]
    for name, exception, frames in failures:
        lines.append("_" * 20 + f" {name} " + "_" * 20)
        lines.extend(frame.format(name=name) for frame in frames)
        lines.extend(
            f"    copy_function(srcentry, dstname)  # traceback frame {index}"
            for index in range(filler)
        )
        lines.append(f"E   {exception}")
    lines.append("=" * 20 + " short test summary info " + "=" * 20)
    lines.extend(
        f"FAILED tests/test_links.py::{name} - {exception}"
        for name, exception, _frames in failures
    )
    lines.append(f"{len(failures)} failed, {passed} passed in 0.42s")
    return "\n".join(lines) + "\n" + trailer


def _row(
    stdout: str, *, stderr: str = "", returncode: int = 1,
    backend: str = "windows_appcontainer",
) -> dict:
    """One validation receipt row exactly as ``run_validations`` bounds it."""
    row = {
        "command": _COMMAND,
        "declared_command": _COMMAND,
        "argv": ["python", "-m", "pytest", "-q", _OUTPUT],
        "executed_argv": ["python", "-m", "pytest", "-q", _OUTPUT],
        "sandbox_backend": backend,
        "returncode": returncode,
        "timed_out": False,
        "stdout_head": stdout[:4_096],
        "stdout_tail": stdout[-4_096:],
        "stdout_truncated": len(stdout) > 8_192,
        "stderr_head": stderr[:4_096],
        "stderr_tail": stderr[-4_096:],
        "stderr_truncated": len(stderr) > 8_192,
    }
    row.update(validation_runner.pytest_validation_evidence(stdout, returncode))
    return row


def _winerror_row() -> dict:
    return _row(
        _pytest_stdout([("test_link_target", _WINERROR, _SYMLINK_FRAMES)]),
        stderr=_WINERROR + "\n",
    )


def _typeerror_row() -> dict:
    return _row(_pytest_stdout([("test_parse", _TYPEERROR, _SYMLINK_FRAMES)]))


def _semlock_row(stdout: str) -> dict:
    # NF690 measured the filename-less SemLock denial in the landlock lane.
    return _row(stdout, backend="landlock")


def _records(path: str = _OUTPUT, digest: object = _DIGEST) -> list[dict]:
    return [
        {"pattern": path, "path": path, "bytes": 42, "sha256": digest,
         "unchanged_allowed": False}
    ]


def _disposition(rows, *, records=None, terminal_state="validation_failed"):
    return tfc.validation_environment_disposition(
        terminal_state=terminal_state,
        validations=rows,
        required_outputs=[_OUTPUT],
        required_output_records=_records() if records is None else records,
        task_id=_TASK_ID,
    )


# --- the positive case ------------------------------------------------------ #


def test_a_winerror_1314_row_is_classified_validation_environment_with_the_plan():
    row = _winerror_row()
    assert row["returncode"] == 1 and "WinError 1314" in row["stderr_tail"]
    assert "AssertionError" not in json.dumps(row)

    disposition = _disposition([row])

    assert disposition is not None
    assert disposition["schema_id"] == tfc.DISPOSITION_SCHEMA_ID
    assert disposition["cause"] == tfc.CAUSE_VALIDATION_ENVIRONMENT
    assert tfc.failure_category_projection(disposition) == "validation_environment"
    assert disposition["evidence"] == f"validation_environment_signature={_SYMLINK}"
    # Candidate output is no trusted channel: the cause is classified, the next
    # step stays a manager's and starts nothing.
    assert disposition["evidence_source"] == "candidate_validation_output"
    assert disposition["candidate_forgeable"] is True
    assert disposition["evidence_authority"] == tfc.EVIDENCE_AUTHORITY_NONE
    assert disposition["action"] == tfc.ACTION_MANAGER_JUDGMENT_UNKNOWN
    assert disposition["provider_launched"] is False
    assert disposition["candidate_bytes_preserved"] is True

    recovery = disposition["validation_environment_recovery"]
    assert recovery["schema_id"] == tfc.VALIDATION_ENVIRONMENT_RECOVERY_SCHEMA_ID
    assert recovery["signature"] == _SYMLINK
    assert recovery["signature_version"] == (
        validation_runner.VALIDATION_ENVIRONMENT_SIGNATURES_VERSION
    )
    assert recovery["command"] == _COMMAND
    assert recovery["sandbox_backend"] == "windows_appcontainer"
    assert recovery["evidence_source"] == "candidate_validation_output"
    assert recovery["candidate_forgeable"] is True
    assert recovery["host_execution"] is False
    assert recovery["lane_change_available"] is False
    assert recovery["lane_change_reason"] == (
        "compatible_validation_lane_unavailable:backend=windows_appcontainer:"
        "privilege=SeCreateSymbolicLinkPrivilege"
    )
    assert recovery["plan"] == [
        {
            "tool": "aiworkhub_task_reject_review",
            "arguments": {
                "task_id": _TASK_ID,
                "reason": f"validation_environment_signature={_SYMLINK}",
                "to": "blocked",
                "failure_category": "validation_environment",
            },
        },
        {
            "tool": "aiworkhub_task_recover_blocked_rework",
            "arguments": {"task_id": _TASK_ID, "validation_only_replay": True},
        },
    ]


def test_the_signature_table_is_closed_versioned_and_the_match_is_frozen():
    signatures = validation_runner.VALIDATION_ENVIRONMENT_SIGNATURES
    assert isinstance(signatures, tuple)
    assert [signature.name for signature in signatures] == [_SYMLINK, _SEMLOCK]
    assert validation_runner.VALIDATION_ENVIRONMENT_SIGNATURES_VERSION == (
        "aiworkhub.validation_environment_signatures.v1"
    )
    assert all(signature.lane_change_reason for signature in signatures)

    match = validation_runner.match_validation_environment_signature(
        [{"command": "ruff check src", "returncode": 0}, _winerror_row()],
        required_outputs=[_OUTPUT],
        required_output_records=_records(),
    )
    assert match == validation_runner.ValidationEnvironmentSignatureMatch(
        signature=_SYMLINK, command=_COMMAND, sandbox_backend="windows_appcontainer"
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        match.signature = _SEMLOCK  # type: ignore[misc]


def test_a_semlock_denial_matches_only_filename_less_with_its_frame_in_view():
    semlock = ("test_pool", _SEMLOCK_DENIED, _SEMLOCK_FRAMES)
    assert validation_runner.validation_environment_signature(
        _semlock_row(_pytest_stdout([semlock]))
    ) == _SEMLOCK

    # A file-permission failure names its file, so it is the candidate's own.
    file_denied = ("test_state", _SEMLOCK_DENIED + ": '/srv/state.json'", _SYMLINK_FRAMES)
    assert validation_runner.validation_environment_signature(
        _semlock_row(_pytest_stdout([semlock, file_denied]))
    ) is None
    # A filename-less denial with no SemLock frame in view names nothing.
    bare = ("test_socket", _SEMLOCK_DENIED, _SYMLINK_FRAMES)
    assert validation_runner.validation_environment_signature(
        _semlock_row(_pytest_stdout([bare]))
    ) is None


def test_one_semlock_frame_never_vouches_for_another_filename_less_denial():
    row = _semlock_row(
        _pytest_stdout(
            [
                ("test_pool", _SEMLOCK_DENIED, _SEMLOCK_FRAMES),
                ("test_socket", _SEMLOCK_DENIED, _SYMLINK_FRAMES),
            ]
        )
    )
    assert validation_runner.validation_environment_signature(row) is None
    assert _disposition([row]) is None


def test_every_semlock_block_holds_its_own_frame_across_entry_separators():
    # ``--tb=long`` separates traceback entries with ``_ _ _``: no block header.
    frames = _SEMLOCK_FRAMES[:2] + ("_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _",) + _SEMLOCK_FRAMES[2:]
    row = _semlock_row(
        _pytest_stdout(
            [("test_pool", _SEMLOCK_DENIED, frames), ("test_queue", _SEMLOCK_DENIED, frames)]
        )
    )
    assert validation_runner.validation_environment_signature(row) == _SEMLOCK


def test_a_trimmed_semlock_summary_line_is_proved_by_its_own_block():
    stdout = _pytest_stdout([("test_pool_map", _SEMLOCK_DENIED, _SEMLOCK_FRAMES)])
    stdout = stdout.replace(
        f"::test_pool_map - {_SEMLOCK_DENIED}",
        "::test_pool_map - PermissionError: [Errno 13] Permi...",
    )
    assert validation_runner.validation_environment_signature(_semlock_row(stdout)) == _SEMLOCK


@pytest.mark.parametrize("backend", ["landlock", "bubblewrap", "", None])
def test_a_winerror_1314_row_from_another_lane_is_never_classified(backend):
    row = {**_winerror_row(), "sandbox_backend": backend}
    assert validation_runner.validation_environment_signature(row) is None
    assert validation_runner.match_validation_environment_signature(
        [row], required_outputs=[_OUTPUT], required_output_records=_records()
    ) is None
    assert _disposition([row]) is None


@pytest.mark.parametrize("backend", ["windows_appcontainer", "bubblewrap", "", None])
def test_a_semlock_row_from_another_lane_is_never_classified(backend):
    stdout = _pytest_stdout([("test_pool", _SEMLOCK_DENIED, _SEMLOCK_FRAMES)])
    row = {**_semlock_row(stdout), "sandbox_backend": backend}
    assert validation_runner.validation_environment_signature(row) is None
    assert _disposition([row]) is None


# --- assertion markers ------------------------------------------------------ #


@pytest.mark.parametrize(
    "field",
    ["stdout_tail", "stdout_head", "stderr_tail", "stderr_head", "pytest_first_failure"],
)
def test_an_assertion_that_mentions_winerror_1314_is_never_classified(field):
    row = _winerror_row()
    assert validation_runner.validation_environment_signature(row) == _SYMLINK
    row[field] = (
        str(row.get(field) or "")
        + "\nE   AssertionError: expected [WinError 1314] to skip, not fail\n"
    )
    assert validation_runner.validation_environment_signature(row) is None
    assert _disposition([row]) is None


@pytest.mark.parametrize(
    "exception",
    [
        "assert '[WinError 1314]' in 'privilege held'",
        "Failed: DID NOT RAISE <class 'OSError'> [WinError 1314]",
    ],
)
def test_a_rewritten_assert_or_pytest_fail_carrying_the_literal_is_not_classified(
    exception,
):
    row = _row(_pytest_stdout([("test_link_target", exception, _SYMLINK_FRAMES)]))
    assert "[WinError 1314]" in row["stdout_tail"]
    assert validation_runner.validation_environment_signature(row) is None
    assert _disposition([row]) is None


# --- one row runs many tests ------------------------------------------------ #


def test_one_winerror_1314_test_never_masks_a_typeerror_in_the_same_row():
    row = _row(
        _pytest_stdout(
            [
                ("test_link_target", _WINERROR, _SYMLINK_FRAMES),
                ("test_parse", _TYPEERROR, _SYMLINK_FRAMES),
            ]
        ),
        stderr=_WINERROR + "\n",
    )
    assert validation_runner.validation_environment_signature(row) is None
    assert _disposition([row]) is None


def test_a_summary_line_pytest_trimmed_of_its_message_is_proved_by_its_block():
    # pytest trims the message to the terminal width with ``...`` and drops it
    # when the node id leaves no room; the block's ``E`` line still carries it.
    stdout = _pytest_stdout([("test_link_target", _WINERROR, _SYMLINK_FRAMES)])
    summary = f"FAILED tests/test_links.py::test_link_target - {_WINERROR}"
    for trimmed in (
        "FAILED tests/test_links.py::test_link_target",
        "FAILED tests/test_links.py::test_link_target - OSError: [WinError 13...",
    ):
        row = _row(stdout.replace(summary, trimmed))
        assert validation_runner.validation_environment_signature(row) == _SYMLINK


def test_trimmed_summary_lines_whose_blocks_are_out_of_view_fail_closed():
    row = _row(_pytest_stdout(_many(20), filler=10).replace(f" - {_WINERROR}", ""))
    assert row["stdout_truncated"] is True
    assert "short test summary info" in row["stdout_tail"]
    assert validation_runner.validation_environment_signature(row) is None
    assert _disposition([row]) is None


@pytest.mark.parametrize(
    "stdout, stderr",
    [
        # A run that died before pytest wrote its short summary.
        ("tests/test_links.py:12: in test_link_target\nE   " + _WINERROR + "\n", ""),
        # A non-pytest command: the signature is prose on stderr.
        ("", "Traceback (most recent call last):\n" + _WINERROR + "\n"),
    ],
)
def test_a_nonzero_row_with_no_short_summary_line_fails_closed(stdout, stderr):
    row = _row(stdout, stderr=stderr)
    assert validation_runner.validation_environment_signature(row) is None
    assert _disposition([row]) is None


def test_an_uncut_summary_that_lists_fewer_failures_than_it_counts_fails_closed():
    # ``-rf --tb=no`` lists the failure and prints neither line for two errors.
    stdout = (
        "...FEE    [100%]\n"
        + "=" * 20 + " short test summary info " + "=" * 20 + "\n"
        + f"FAILED tests/test_links.py::test_link_target - {_WINERROR}\n"
        + "1 failed, 3 passed, 2 errors in 0.42s\n"
    )
    row = _row(stdout)
    assert row["stdout_truncated"] is False and row["stdout_head"] == row["stdout_tail"]
    assert validation_runner.validation_environment_signature(row) is None
    assert _disposition([row]) is None


def test_every_failing_row_must_match_and_a_timed_out_row_never_does():
    assert _disposition([_winerror_row(), _typeerror_row()]) is None
    timed_out = {**_winerror_row(), "returncode": None, "timed_out": True}
    assert _disposition([timed_out]) is None
    assert _disposition([{**_winerror_row(), "returncode": 0}]) is None


# --- truncation ------------------------------------------------------------- #


def _many(count, *, odd_one_out=None):
    failures = [
        (f"test_link_{index:02d}", _WINERROR, _SYMLINK_FRAMES) for index in range(count)
    ]
    if odd_one_out is not None:
        failures[odd_one_out] = (f"test_link_{odd_one_out:02d}", _TYPEERROR, _SYMLINK_FRAMES)
    return failures


def test_a_truncated_row_matches_only_when_its_closing_block_is_wholly_in_view():
    row = _row(_pytest_stdout(_many(20), filler=10))
    assert row["stdout_truncated"] is True
    assert "short test summary info" in row["stdout_tail"]
    assert validation_runner.validation_environment_signature(row) == _SYMLINK


def test_a_truncated_row_whose_windows_lack_the_final_failed_line_is_not_classified():
    stdout = _pytest_stdout(
        [("test_link_target", _WINERROR, _SYMLINK_FRAMES)],
        trailer="teardown: releasing fixture resources\n" * 300,
    )
    row = _row(stdout)
    assert row["stdout_truncated"] is True
    assert "1 failed, 3 passed" not in row["stdout_tail"]
    assert validation_runner.validation_environment_signature(row) is None
    assert _disposition([row]) is None


def test_a_truncated_row_whose_summary_block_straddles_the_cut_is_not_classified():
    row = _row(_pytest_stdout(_many(40), filler=10))
    assert row["stdout_truncated"] is True
    assert "40 failed" in row["stdout_tail"]
    assert "short test summary info" not in row["stdout_tail"]
    assert validation_runner.validation_environment_signature(row) is None


def test_a_failure_hidden_in_the_cut_still_unmasks_through_its_summary_line():
    row = _row(_pytest_stdout(_many(20, odd_one_out=9), filler=10))
    assert row["stdout_truncated"] is True
    assert _TYPEERROR not in row["stdout_head"]
    assert validation_runner.validation_environment_signature(row) is None


# --- provenance and inputs ------------------------------------------------- #


def test_a_signature_only_in_worker_prose_or_the_error_string_never_classifies():
    row = _typeerror_row()
    row["launch_error_message"] = _WINERROR
    row["failure_receipt"] = {"summary": _WINERROR}
    row["worker_summary"] = "worker prose: the sandbox hit [WinError 1314]"
    metadata = {
        "task_id": _TASK_ID,
        "required_outputs": [_OUTPUT],
        "objective": "fix the [WinError 1314] symlink test",
    }
    assert validation_runner.validation_environment_signature(row) is None
    assert validation.validation_environment_evidence(
        "validation_failed", metadata, [row], _records()
    ) == {}


@pytest.mark.parametrize(
    "records",
    [
        [],
        _records(digest=None),
        _records(digest="symlink:elsewhere"),
        _records(path="docs/other.md"),
        [_OUTPUT],
    ],
    ids=["unrecorded", "no_hash", "not_a_digest", "other_path", "malformed"],
)
def test_a_missing_required_output_never_classifies(records):
    row = _winerror_row()
    assert validation_runner.validation_environment_signature(row) == _SYMLINK
    assert _disposition([row], records=records) is None


@pytest.mark.parametrize(
    "terminal_state", ["finalize_failed", "review_ready", "scope_rejected", ""]
)
def test_only_validation_failed_is_ever_placed(terminal_state):
    assert _disposition([_winerror_row()], terminal_state=terminal_state) is None


def test_classification_launches_nothing(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("classification must never start a process")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(os, "system", refuse)
    metadata = {"task_id": _TASK_ID, "required_outputs": [_OUTPUT]}

    evidence = validation.validation_environment_evidence(
        "validation_failed", metadata, [_winerror_row()], _records()
    )

    recovery = evidence["validation_environment_recovery"]
    assert recovery["host_execution"] is False
    assert evidence["validation_environment_disposition"]["provider_launched"] is False
    assert "manager_host" not in json.dumps(evidence)


def test_the_evidence_helper_never_lets_an_exception_reach_the_terminal(monkeypatch):
    def broken(**_kwargs):
        raise RuntimeError("classifier defect")

    monkeypatch.setattr(tfc, "validation_environment_disposition", broken)
    metadata = {"task_id": _TASK_ID, "required_outputs": [_OUTPUT]}
    assert validation.validation_environment_evidence(
        "validation_failed", metadata, [_winerror_row()], _records()
    ) == {}


# --- the stamp on the row ---------------------------------------------------- #


def _bare_workspace(tmp_path: Path) -> worker_workspace.WorkerWorkspace:
    return worker_workspace.WorkerWorkspace(
        request_id="req-nf01671-stamp",
        repo=tmp_path / "repo",
        path=tmp_path / "workspace",
        home=tmp_path / "home",
        allowed_writes=(_OUTPUT,),
        parent_baseline={},
        workspace_baseline={},
    )


def _declared_failure(tmp_path: Path, row: dict) -> ValidationRunError:
    def run(_workspace, _commands, **_route):
        raise ValidationRunError("validation_failed:command_failed", [row])

    def ineligible(*_args, **_kwargs):
        raise WorkspaceError("baseline_comparison_ineligible")

    with pytest.raises(ValidationRunError) as caught:
        validation.run_declared_validations(
            _bare_workspace(tmp_path),
            {"validation": [_COMMAND]},
            {},
            run_validations=run,
            route_resolver=lambda _metadata: {},
            baseline_comparer=ineligible,
        )
    return caught.value


def test_run_declared_validations_stamps_the_failed_row_and_keeps_the_verdict(tmp_path):
    exc = _declared_failure(tmp_path, _winerror_row())

    assert not isinstance(exc, ValidationEnvironmentBlocked)
    assert str(exc).startswith("validation_failed:command_failed")
    assert validation.terminal_state_for_workspace_error(exc) == "validation_failed"
    assert exc.results[0]["validation_environment_signature"] == {
        "schema_id": validation_runner.VALIDATION_ENVIRONMENT_SIGNATURES_VERSION,
        "signature": _SYMLINK,
        "evidence_source": "candidate_validation_output",
        "candidate_forgeable": True,
    }


def test_a_non_matching_failed_row_is_left_exactly_as_the_runner_wrote_it(tmp_path):
    plain = _typeerror_row()
    exc = _declared_failure(tmp_path, plain)

    assert validation.terminal_state_for_workspace_error(exc) == "validation_failed"
    assert {
        key: value for key, value in exc.results[0].items() if key != "behavioral_role"
    } == plain


def test_a_signature_classifier_fault_never_replaces_the_measured_failure(
    tmp_path, monkeypatch
):
    def broken(_row):
        raise RuntimeError("classifier defect")

    monkeypatch.setattr(
        validation_runner, "validation_environment_signature_evidence", broken
    )
    row = _winerror_row()
    exc = _declared_failure(tmp_path, row)

    assert type(exc) is ValidationRunError
    assert str(exc).startswith("validation_failed:command_failed")
    assert validation.terminal_state_for_workspace_error(exc) == "validation_failed"
    assert {
        key: value for key, value in exc.results[0].items() if key != "behavioral_role"
    } == row


# --- the finalizer ----------------------------------------------------------- #


def _finalize(tmp_path: Path, monkeypatch, rows, *, request_id: str, message: str):
    """Drive ``_finalize_isolated_request`` to its ``except WorkspaceError`` branch.

    The worker's one change is its declared output, so git scope detection and
    the anchored candidate seal are stubbed; the declared-validation path is
    the real ``run_declared_validations`` with only the sandbox run replaced by
    its failed receipt.
    """
    monkeypatch.setenv("AIWORKHUB_TOOLCHAIN_AUTHORITY_HMAC_KEY", "hex:" + "11" * 32)
    monkeypatch.setenv(process_launcher.ALLOW_LAUNCH_ENV, "1")
    monkeypatch.setenv(process_launcher.ALLOW_WRITES_ENV, "1")

    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / _OUTPUT).write_text("canonical\n", encoding="utf-8")
    workspace_dir = tmp_path / "workspace"
    (workspace_dir / "tests").mkdir(parents=True)
    output = workspace_dir / _OUTPUT
    output.write_text("canonical\n", encoding="utf-8")
    baseline = worker_workspace._hash_path(output)
    output.write_text("candidate\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    workspace = worker_workspace.WorkerWorkspace(
        request_id=request_id,
        repo=repo,
        path=workspace_dir,
        home=home,
        allowed_writes=(_OUTPUT,),
        parent_baseline={_OUTPUT: baseline},
        workspace_baseline={_OUTPUT: baseline},
    )
    card = {
        "task_id": _TASK_ID,
        "runner": _RUNNER,
        "topic": "task_mcp",
        "status": "processing",
        "worker_status": "claimed",
        "claimed_by": _RUNNER,
    }

    def show(task_id: str) -> dict:
        assert task_id == _TASK_ID
        return {"returncode": 0, "stdout": json.dumps(card), "stderr": ""}

    manager = process_launcher.ProcessManager(
        repo=repo,
        process_log_path=tmp_path / "events.jsonl",
        process_dir=tmp_path / "processes",
        show_task=show,
        collision_guard=lambda **_kwargs: {
            "returncode": 0, "stdout": '{"collision_free":true}', "stderr": "",
        },
        adapter_builder=lambda *_args, **_kwargs: {
            "argv": [sys.executable, "-c", "pass"], "cwd": str(repo),
        },
        isolation_enabled=False,
    )
    calls: list[tuple[str, str, dict]] = []

    def record(kind):
        def terminal(_metadata, substatus, **kwargs):
            calls.append((kind, substatus, dict(kwargs.get("evidence") or {})))
            return {"ok": True, "returncode": 0, "stdout": "{}", "stderr": ""}

        return terminal

    monkeypatch.setattr(manager, "_terminal_failure_exact", record("failure"))
    monkeypatch.setattr(manager, "_review_terminal_exact", record("review"))
    monkeypatch.setattr(process_launcher, "enforce_scope", lambda *_a, **_k: [_OUTPUT])
    monkeypatch.setattr(
        process_launcher, "_retained_candidate_seal_evidence", lambda *_a, **_k: {}
    )

    def full_snapshot(target, authority, route_metadata, _paths):
        def run(_workspace, _commands, **_route):
            raise ValidationRunError(message, rows)

        return validation.run_declared_validations(
            target,
            authority,
            route_metadata,
            run_validations=run,
            route_resolver=lambda _metadata: {},
            baseline_comparer=process_launcher._compare_schema_mypy_baseline,
        ), {}

    monkeypatch.setattr(process_launcher, "_run_full_snapshot_validations", full_snapshot)

    stdout_path = tmp_path / f"{request_id}.stdout.log"
    stderr_path = tmp_path / f"{request_id}.stderr.log"
    stdout_path.write_text("worker complete\n", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    status_path = tmp_path / f"{request_id}.supervisor.json"
    worker_workspace.write_json_0600(status_path, {"state": "exited", "exit_code": 0})
    metadata_path = tmp_path / f"{request_id}.request.json"
    metadata_path.write_text(
        json.dumps(
            {
                "schema_id": "aiworkhub.task_mcp.isolated_request.v1",
                "request_id": request_id,
                "task_id": _TASK_ID,
                "runner": _RUNNER,
                "topic": "task_mcp",
                "adapter_id": "claude_cli",
                "model": "claude_cli",
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "supervisor_status_path": str(status_path),
                "cancel_path": str(tmp_path / f"{request_id}.cancel.json"),
                "prompt_sha256": "0" * 64,
                "project_context": None,
                "project_context_delivery": {"injected": False},
                "sandbox_backend": "landlock",
                "required_outputs": [_OUTPUT],
                "allow_empty_required_outputs": [],
                "allow_unchanged_required_outputs": [],
                "external_readonly_dirs": [],
                "workspace": workspace.as_metadata(),
                "claim_epoch": 1,
                "validation": [_COMMAND],
            }
        ),
        encoding="utf-8",
    )
    manager._append_event(
        {
            "request_id": request_id,
            "task_id": _TASK_ID,
            "runner": _RUNNER,
            "topic": "task_mcp",
            "adapter_id": "claude_cli",
            "model": "claude_cli",
            "state": "running",
            "pid": 999_999_999,
            "pid_start_ticks": 1,
            "metadata_path": str(metadata_path),
            "supervisor_status_path": str(status_path),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }
    )
    event = manager._finalize_isolated_request(request_id, supervisor_returncode=0)
    return event, calls


def test_finalize_attaches_the_recovery_and_disposition_and_keeps_todays_routing(
    tmp_path, monkeypatch
):
    event, calls = _finalize(
        tmp_path / "match", monkeypatch, [_winerror_row()],
        request_id="req-nf01671-match", message="validation_failed:command_failed",
    )
    plain_event, plain_calls = _finalize(
        tmp_path / "plain", monkeypatch, [_typeerror_row()],
        request_id="req-nf01671-plain",
        message="validation_failed:command_failed [WinError 1314] in the error string",
    )

    # Today's routing for both: a candidate gate failure goes to review.
    assert event["state"] == plain_event["state"] == "validation_failed"
    assert [(kind, state) for kind, state, _ in calls] == [("review", "validation_failed")]
    assert [(kind, state) for kind, state, _ in plain_calls] == [
        ("review", "validation_failed")
    ]

    evidence = calls[0][2]
    disposition = evidence["validation_environment_disposition"]
    assert disposition["cause"] == tfc.CAUSE_VALIDATION_ENVIRONMENT
    assert disposition["evidence_source"] == "candidate_validation_output"
    assert disposition["candidate_forgeable"] is True
    assert disposition["provider_launched"] is False
    assert "validation_environment_recovery" not in disposition
    recovery = evidence["validation_environment_recovery"]
    assert recovery["signature"] == _SYMLINK
    assert recovery["command"] == _COMMAND
    assert recovery["host_execution"] is False
    assert [step["arguments"]["task_id"] for step in recovery["plan"]] == [_TASK_ID] * 2
    assert event["validation"][0]["validation_environment_signature"]["signature"] == _SYMLINK

    # A non-matching failure is unchanged: the same evidence keys minus these
    # two, and its row carries no stamp.
    plain_evidence = plain_calls[0][2]
    assert "validation_environment_recovery" not in plain_evidence
    assert "validation_environment_disposition" not in plain_evidence
    assert set(evidence) - set(plain_evidence) == {
        "validation_environment_recovery",
        "validation_environment_disposition",
    }
    assert set(plain_evidence) <= set(evidence)
    assert "validation_environment_signature" not in plain_event["validation"][0]


def test_finalize_routing_survives_a_classifier_defect(tmp_path, monkeypatch):
    def broken(**_kwargs):
        raise RuntimeError("classifier defect")

    monkeypatch.setattr(tfc, "validation_environment_disposition", broken)
    event, calls = _finalize(
        tmp_path, monkeypatch, [_winerror_row()],
        request_id="req-nf01671-defect", message="validation_failed:command_failed",
    )
    assert event["state"] == "validation_failed"
    assert [(kind, state) for kind, state, _ in calls] == [("review", "validation_failed")]
    assert "validation_environment_recovery" not in calls[0][2]
    assert "validation_environment_disposition" not in calls[0][2]


def test_process_launcher_line_count_does_not_grow():
    source = Path(process_launcher.__file__).read_text(encoding="utf-8")
    assert len(source.splitlines()) <= _PROCESS_LAUNCHER_LINE_CEILING
