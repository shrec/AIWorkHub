"""NF-2026-02069: neighbours run at finalization with an explicit width; accept reuses them."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aiworkhub import accept_neighbour_tests as neighbours
from aiworkhub import process_launcher_accept_review as accept_review
from aiworkhub import process_launcher_validation as validation
from aiworkhub.worker_workspace import WorkspaceError

_DECLARED = "python -m pytest tests/test_declared.py -q"
_AUTHORITY = {"allowed_writes": ["src/aiworkhub/engine.py"], "validation": [_DECLARED]}
_CANDIDATE = ["src/aiworkhub/engine.py"]
_TESTS = ("tests/test_b.py", "tests/test_a.py")
_SELECTION = neighbours.NeighbourSelection(
    tests=_TESTS, reason="", limit=12,
    production_paths=("src/aiworkhub/engine.py",), related_count=2,
)
_GREEN = {"returncode": 0, "timed_out": False, "stdout_tail": "2 passed"}
_RED = {"returncode": 1, "timed_out": False, "stdout_tail": "boom"}


def _cores(monkeypatch: pytest.MonkeyPatch, count: int | None) -> None:
    monkeypatch.setattr(neighbours.os, "cpu_count", lambda: count)


# --- the explicit width ---------------------------------------------------------------


def _files(count: int) -> list[str]:
    return [f"tests/test_n{index:02d}.py" for index in range(count)]


@pytest.mark.parametrize(
    ("cores", "files", "width"),
    [
        (16, 12, 12), (16, 20, 12), (16, 3, 3),
        (4, 12, 2), (4, 1, 1),
        (1, 12, 1), (None, 12, 1), (2, 12, 1), (8, 12, 6),
    ],
)
def test_width_is_cores_minus_headroom_capped_by_files(
    monkeypatch: pytest.MonkeyPatch, cores: int | None, files: int, width: int
) -> None:
    _cores(monkeypatch, cores)
    tests = _files(files)
    assert neighbours.neighbour_width(tests) == width
    command = neighbours.neighbour_command(tests)
    assert command == f"python -m pytest -n {width} " + " ".join(tests) + " -q"
    assert command.count(" -n ") == 1


def test_width_counts_distinct_files_not_node_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    _cores(monkeypatch, 16)
    nodes = ["tests/test_a.py::test_x", "tests/test_a.py::test_y", "tests/test_b.py"]
    assert neighbours.neighbour_width(nodes) == 2


def test_cores_are_sampled_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    tests = _files(12)
    _cores(monkeypatch, 16)
    assert " -n 12 " in neighbours.neighbour_command(tests)
    _cores(monkeypatch, 4)
    assert " -n 2 " in neighbours.neighbour_command(tests)


def test_serial_keeps_n_zero_and_no_second_width(monkeypatch: pytest.MonkeyPatch) -> None:
    _cores(monkeypatch, 16)
    command = neighbours.neighbour_command(list(_TESTS), serial=True)
    assert command == "python -m pytest -n 0 tests/test_b.py tests/test_a.py -q"
    assert command.count("-n ") == 1


# --- finalization -----------------------------------------------------------------------


class _Snapshot:
    """Fake run_full_snapshot_validations collaborators; records every call."""

    def __init__(self, tmp_path: Path, *, declared: dict[str, Any], neighbour: Any) -> None:
        self.workspace = SimpleNamespace(
            request_id="req-1", repo=tmp_path / "repo", base_oid="b" * 40,
            path=tmp_path / "ws", home=tmp_path / "home",
        )
        self.snapshot = SimpleNamespace(
            request_id="req-1", repo=tmp_path / "repo", base_oid="s" * 40,
            path=tmp_path / "snap", home=tmp_path / "snap-home",
        )
        self.declared = declared
        self.neighbour = neighbour
        self.runs: list[tuple[Any, list[str]]] = []
        self.cleaned = 0

    def create(self, _workspace: Any, _authority: Any, _candidate: Any) -> Any:
        return self.snapshot, {"merged_paths": {}}

    def cleanup(self, *_args: Any) -> None:
        self.cleaned += 1

    def run_validations(self, workspace: Any, commands: list[str], **_route: Any) -> Any:
        self.runs.append((workspace, list(commands)))
        if commands == [_DECLARED]:
            return [{"command": _DECLARED, **self.declared}]
        if isinstance(self.neighbour, BaseException):
            raise self.neighbour
        return [{"command": commands[0], **self.neighbour}]

    def call(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return validation.run_full_snapshot_validations(
            self.workspace, _AUTHORITY, {}, _CANDIDATE,
            create_snapshot=self.create,
            cleanup_workspace=self.cleanup,
            run_validations=self.run_validations,
            route_resolver=lambda _metadata: {},
            baseline_comparer=lambda *_a, **_k: [],
        )


class _Select:
    def __init__(self, selection: neighbours.NeighbourSelection) -> None:
        self.selection = selection
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, repo: Any, changed: Any, commands: Any, **_kw: Any) -> Any:
        self.calls.append((repo, list(changed), list(commands)))
        return self.selection


@pytest.fixture()
def select(monkeypatch: pytest.MonkeyPatch) -> _Select:
    _cores(monkeypatch, 16)
    fake = _Select(_SELECTION)
    monkeypatch.setattr(neighbours, "select_neighbour_tests", fake)
    return fake


def test_green_finalization_records_neighbour_evidence_in_the_snapshot(
    tmp_path: Path, select: _Select
) -> None:
    fake = _Snapshot(tmp_path, declared=_GREEN, neighbour=_GREEN)
    rows, record = fake.call()
    assert rows[0]["command"] == _DECLARED
    assert record["validations_all_passed"] is True
    evidence = record["neighbour_tests"]
    assert evidence["passed"] is True
    assert evidence["selected"] == list(_TESTS)
    assert evidence["command"] == "python -m pytest -n 2 tests/test_b.py tests/test_a.py -q"
    assert isinstance(evidence["duration_seconds"], float)
    assert "neighbour_flaky_rerun" not in evidence
    # Selected against the canonical repo, run once in the same snapshot, same runner.
    assert select.calls == [(fake.workspace.repo, _CANDIDATE, [_DECLARED])]
    assert [(ws, cmds) for ws, cmds in fake.runs] == [
        (fake.snapshot, [_DECLARED]), (fake.snapshot, [evidence["command"]]),
    ]
    assert fake.cleaned == 1


def test_failed_declared_validation_runs_no_neighbours(
    tmp_path: Path, select: _Select
) -> None:
    fake = _Snapshot(tmp_path, declared=_RED, neighbour=_GREEN)
    _rows, record = fake.call()
    assert record["validations_all_passed"] is False
    assert "neighbour_tests" not in record
    assert select.calls == [] and len(fake.runs) == 1


def test_empty_selection_runs_no_neighbours(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, select: _Select
) -> None:
    select.selection = neighbours.NeighbourSelection(tests=(), reason="no_related_tests", limit=12)
    fake = _Snapshot(tmp_path, declared=_GREEN, neighbour=_GREEN)
    _rows, record = fake.call()
    assert "neighbour_tests" not in record
    assert len(select.calls) == 1 and len(fake.runs) == 1


@pytest.mark.parametrize(
    "neighbour",
    [_RED, WorkspaceError("validation_environment_blocked:x"), RuntimeError("runner crashed")],
    ids=["red_rows", "workspace_error", "unexpected_error"],
)
def test_red_or_erroring_neighbours_are_recorded_not_raised(
    tmp_path: Path, select: _Select, neighbour: Any
) -> None:
    fake = _Snapshot(tmp_path, declared=_GREEN, neighbour=neighbour)
    rows, record = fake.call()
    assert record["validations_all_passed"] is True
    evidence = record["neighbour_tests"]
    assert evidence["passed"] is False
    assert evidence["selected"] == list(_TESTS)
    assert evidence["error"]
    assert evidence["command"].startswith("python -m pytest -n 2 ")
    assert fake.cleaned == 1


# --- accept-time reuse -------------------------------------------------------------------


def _finalizer_record(**neighbour: Any) -> dict[str, Any]:
    evidence = {"selected": list(_TESTS), "command": "c", "passed": True, **neighbour}
    return {
        "schema_id": "aiworkhub.full_validation_snapshot.v1",
        "request_id": "req-1",
        "source_base_oid": "b" * 40,
        "tree_identity_sha256": "id-1",
        "validations_all_passed": True,
        "neighbour_tests": evidence,
    }


def _terminal(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "evidence": {
            "validation": [{"command": _DECLARED, "returncode": 0, "timed_out": False}],
            "quality_gate": {"full_validation_snapshot": record},
        }
    }


def _union(monkeypatch: pytest.MonkeyPatch, identity: str, reuse: Any) -> tuple[Any, list[int]]:
    """The accept's union decision, through the real run_union_validations_once."""
    monkeypatch.setattr(validation, "combined_tree_identity", lambda *a, **k: identity)
    ran: list[int] = []

    def run_declared() -> list[dict[str, Any]]:
        ran.append(1)
        return [{"command": _DECLARED, "returncode": 0, "timed_out": False}]

    workspace = SimpleNamespace(request_id="req-1", repo="repo", base_oid="b" * 40)
    _rows, union_reuse = validation.run_union_validations_once(
        workspace, _AUTHORITY, {}, _CANDIDATE,
        combined_tree={"merged_paths": {}}, snapshot_base_oid="s" * 40,
        run_declared=run_declared, route_resolver=lambda _m: {}, reuse=reuse,
    )
    return union_reuse, ran


def _accept_neighbours(finalizer_reuse: Any, union_reuse: Any) -> tuple[Any, list[int]]:
    """Mirror of the accept call site: reuse first, else run_neighbour_tests."""
    calls: list[int] = []

    def run(_ws: Any, _authority: Any, _meta: Any) -> list[dict[str, Any]]:
        calls.append(1)
        return [{"command": "x", "returncode": 0, "timed_out": False}]

    evidence = neighbours.reused_finalization_neighbours(
        finalizer_reuse, union_reuse, _SELECTION
    ) or neighbours.run_neighbour_tests(
        _SELECTION, _AUTHORITY, run=run, workspace="union", route_metadata={}
    )
    return evidence, calls


def test_matching_identity_and_selection_reuses_finalization_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _finalizer_record()
    finalizer_reuse = accept_review._finalizer_validation_reuse(
        _terminal(record), correction_record=None
    )
    union_reuse, ran = _union(monkeypatch, "id-1", finalizer_reuse)
    assert ran == []  # the declared validations were reused too
    evidence, calls = _accept_neighbours(finalizer_reuse, union_reuse)
    assert calls == []
    assert evidence["reused_from"] == "finalization"
    assert evidence["selected"] == list(_TESTS) and evidence["passed"] is True


def test_differing_identity_runs_neighbours(monkeypatch: pytest.MonkeyPatch) -> None:
    finalizer_reuse = accept_review._finalizer_validation_reuse(
        _terminal(_finalizer_record()), correction_record=None
    )
    union_reuse, ran = _union(monkeypatch, "id-2", finalizer_reuse)
    assert ran == [1]
    evidence, calls = _accept_neighbours(finalizer_reuse, union_reuse)
    assert calls == [1] and "reused_from" not in evidence


@pytest.mark.parametrize(
    "record",
    [
        {**_finalizer_record(), "neighbour_tests": None},
        {key: value for key, value in _finalizer_record().items() if key != "neighbour_tests"},
        _finalizer_record(passed=False),
        _finalizer_record(passed="true"),
        _finalizer_record(selected=["tests/test_a.py", "tests/test_b.py"]),
        _finalizer_record(selected=["tests/test_b.py"]),
    ],
    ids=["none", "missing", "red", "not_strict_true", "reordered", "subset"],
)
def test_missing_red_or_different_selection_runs_neighbours(
    monkeypatch: pytest.MonkeyPatch, record: dict[str, Any]
) -> None:
    finalizer_reuse = accept_review._finalizer_validation_reuse(
        _terminal(record), correction_record=None
    )
    union_reuse, ran = _union(monkeypatch, "id-1", finalizer_reuse)
    assert ran == []
    evidence, calls = _accept_neighbours(finalizer_reuse, union_reuse)
    assert calls == [1] and "reused_from" not in evidence


def test_manager_correction_runs_neighbours(monkeypatch: pytest.MonkeyPatch) -> None:
    finalizer_reuse = accept_review._finalizer_validation_reuse(
        _terminal(_finalizer_record()), correction_record={"paths": _CANDIDATE}
    )
    assert finalizer_reuse is None
    union_reuse, ran = _union(monkeypatch, "id-1", finalizer_reuse)
    assert ran == [1]
    evidence, calls = _accept_neighbours(finalizer_reuse, union_reuse)
    assert calls == [1] and "reused_from" not in evidence


def test_empty_accept_selection_never_reuses() -> None:
    reuse = {"full_validation_snapshot": _finalizer_record(), "validations": []}
    empty = neighbours.NeighbourSelection(tests=(), reason="x", limit=12)
    assert neighbours.reused_finalization_neighbours(reuse, dict(reuse), empty) is None


def test_accept_site_tries_reuse_before_running() -> None:
    tree = ast.parse(Path(accept_review.__file__).read_text(encoding="utf-8"))
    fn = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "accept_review"
    )
    [lane] = [
        node for node in ast.walk(fn)
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)
        and "run_neighbour_tests" in ast.unparse(node)
    ]
    first, second = lane.values
    assert isinstance(first, ast.Call) and isinstance(second, ast.Call)
    assert ast.unparse(first.func).endswith("reused_finalization_neighbours")
    assert [ast.unparse(arg) for arg in first.args] == [
        "validation_reuse", "union_validation_reuse", "neighbour_selection",
    ]
    assert ast.unparse(second.func).endswith("run_neighbour_tests")
