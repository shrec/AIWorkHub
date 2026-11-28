"""NF-2026-01025: an unrunnable finalization neighbour run records its real cause.

Measured on request 1f26c67f6d9e4c7ea74b14861986c58b: finalization stored
``finalization_neighbour_tests_unrunnable:`` with no usable cause, because the
record kept only the head of the failing output. The record now keeps its TAIL,
the exit code and the exact neighbour command, and a candidate tree that lacks
the AppContainer validation relay module is refused by name before any run.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from aiworkhub import accept_neighbour_tests as neighbours
from aiworkhub import windows_private_session
from aiworkhub.worker_workspace import ValidationRunError, WorkspaceError

_AUTHORITY = {"allowed_writes": ["src/aiworkhub/engine.py"], "validation": ["python -m pytest -q"]}
_CANDIDATE = ["src/aiworkhub/engine.py"]
_TESTS = ("tests/test_hooks.py", "tests/test_other.py")
_SELECTION = neighbours.NeighbourSelection(
    tests=_TESTS, reason="", limit=12,
    production_paths=("src/aiworkhub/engine.py",), related_count=2,
)
_HEAD = "HEAD_OF_OUTPUT_NOT_THE_CAUSE"
_CAUSE = "ModuleNotFoundError: No module named 'aiworkhub.windows_private_session'"


@contextlib.contextmanager
def _no_base(_workspace: Any, _card: Any) -> Iterator[Any]:
    raise WorkspaceError("neighbour_canonical_head_unavailable")
    yield  # pragma: no cover


@pytest.fixture(autouse=True)
def _selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(neighbours, "select_neighbour_tests", lambda *a, **k: _SELECTION)
    monkeypatch.setattr(neighbours, "base_commit_workspace", _no_base)


def _workspace(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        request_id="req-1", repo=tmp_path / "repo", base_oid="s" * 40,
        path=tmp_path / "snap", home=tmp_path / "snap-home",
    )


def _finalize(tmp_path: Path, run: Any, candidate_root: Path | None) -> dict[str, Any]:
    record = neighbours.run_finalization_neighbours(
        tmp_path / "repo", _CANDIDATE, _AUTHORITY, ["python -m pytest -q"],
        run=run, workspace=_workspace(tmp_path), route_metadata={},
        candidate_root=candidate_root,
    )
    assert record is not None
    return record


def _refusal_detail(record: dict[str, Any]) -> dict[str, Any]:
    refusal = neighbours.finalization_neighbour_refusal(record, [])
    assert type(refusal) is WorkspaceError
    text = str(refusal)
    assert text.startswith(neighbours.FINALIZATION_UNRUNNABLE_PREFIX)
    return json.loads(text[len(neighbours.FINALIZATION_UNRUNNABLE_PREFIX):])


def test_unrunnable_record_keeps_output_tail_exit_code_and_command(tmp_path: Path) -> None:
    output = _HEAD + "\n" + "x" * 3000 + "\n" + _CAUSE
    calls: list[Any] = []

    def run(ws: Any, authority: Any, meta: Any) -> list[dict[str, Any]]:
        calls.append(authority)
        raise ValidationRunError(
            "validation_failed:neighbour run produced no verdict",
            [{"command": "neighbours", "returncode": 4, "timed_out": False,
              "stdout_tail": output, "stderr_tail": ""}],
        )

    record = _finalize(tmp_path, run, None)

    assert calls, "the neighbour run was attempted"
    assert record["passed"] is False
    assert record["failure_kind"] == neighbours.FAILURE_KIND_UNRUNNABLE
    assert record["exit_code"] == 4
    assert record["command"] == neighbours.neighbour_command(_TESTS)
    tail = record["output_tail"]
    assert tail.endswith(_CAUSE)
    assert _HEAD not in tail
    assert len(tail) <= neighbours._FIRST_FAILURE_CHARS

    detail = _refusal_detail(record)
    assert detail["exit_code"] == 4
    assert detail["command"] == neighbours.neighbour_command(_TESTS)
    assert detail["output_tail"].endswith(_CAUSE)
    assert detail["failure_kind"] == neighbours.FAILURE_KIND_UNRUNNABLE


def test_unrunnable_cause_falls_back_to_refusal_tail_without_rows() -> None:
    error = _HEAD + "y" * 2000 + _CAUSE
    tail, exit_code = neighbours._unrunnable_cause([], error)
    assert exit_code is None
    assert tail.endswith(_CAUSE) and _HEAD not in tail
    assert len(tail) == neighbours._FIRST_FAILURE_CHARS


def test_relay_module_is_derived_from_relay_entry() -> None:
    module = windows_private_session.validation_relay_module()
    assert module == "aiworkhub.windows_private_session"
    assert module in windows_private_session._VALIDATION_RELAY_ENTRY


def test_candidate_without_relay_module_is_refused_by_name(tmp_path: Path) -> None:
    root = tmp_path / "candidate"
    (root / "src" / "aiworkhub").mkdir(parents=True)
    (root / "src" / "aiworkhub" / "__init__.py").write_text("", encoding="utf-8")

    def run(*_args: Any) -> list[dict[str, Any]]:
        raise AssertionError("neighbours must not run without the relay module")

    record = _finalize(tmp_path, run, root)

    assert record["passed"] is False
    assert record["cause"] == neighbours.VALIDATION_RELAY_MISSING
    assert record["cause"] == "validation_relay_missing_in_candidate"
    assert record["error"] == (
        "validation_relay_missing_in_candidate:src/aiworkhub/windows_private_session.py"
    )
    assert record["command"] == neighbours.neighbour_command(_TESTS)
    detail = _refusal_detail(record)
    assert detail["cause"] == "validation_relay_missing_in_candidate"
    assert "validation_relay_missing_in_candidate" in detail["first_failure"]


def test_candidate_with_relay_module_runs_neighbours(tmp_path: Path) -> None:
    root = tmp_path / "candidate"
    package = root / "src" / "aiworkhub"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "windows_private_session.py").write_text("", encoding="utf-8")
    calls: list[Any] = []

    def run(ws: Any, authority: Any, meta: Any) -> list[dict[str, Any]]:
        calls.append(authority)
        return [{"command": "neighbours", "returncode": 0, "timed_out": False,
                 "stdout_tail": "2 passed"}]

    record = _finalize(tmp_path, run, root)

    assert calls
    assert record["passed"] is True
    assert "cause" not in record


def test_tree_without_the_package_is_not_refused(tmp_path: Path) -> None:
    assert neighbours.relay_missing_in_candidate(tmp_path) == ""
    assert neighbours.relay_missing_in_candidate(None) == ""
