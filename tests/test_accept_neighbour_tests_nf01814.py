"""NF-2026-01814: accept runs the changed modules' neighbour tests on the combined tree.

The selector reuses ``card_contract_advice``'s testmap pass, drops the tests the
card's validation already names, ranks by score with a deterministic tie order
and caps the selection. An unavailable index yields no selection and a named
reason; a red neighbour run refuses with ``neighbour_tests_failed:``.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import accept_neighbour_tests as neighbours
from aiworkhub import card_contract_advice
from aiworkhub import process_launcher_accept_review
from aiworkhub import source_graph as sg
from aiworkhub.toolchain_authority import RECEIPT_CARD_KEY
from aiworkhub.worker_workspace import ValidationRunError, WorkspaceError

ENGINE = "src/tinypkg/engine.py"
HELPERS = "src/tinypkg/helpers.py"
FLOOR = card_contract_advice.RELATED_TEST_SCORE_FLOOR


def _related(*rows: tuple[str, int]) -> dict[str, Any]:
    return {
        "scope": "query_matches",
        "related_tests": [
            {"file_path": path, "score": score, "reasons": []} for path, score in rows
        ],
        "truncated": False,
    }


def _fake_graph(
    monkeypatch: pytest.MonkeyPatch,
    answers: dict[str, Any],
    *,
    freshness: Any = None,
) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    def fake_states(root: Path, paths: Any) -> dict[str, str]:
        if isinstance(freshness, BaseException):
            raise freshness
        return {path: (freshness or {}).get(path, "fresh") for path in paths}

    def fake_query(
        root: Path,
        mode: str,
        query: str,
        budget: int = 64,
        *,
        target: str | None = None,
        cursor: str | None = None,
    ) -> Any:
        calls.append((mode, query, budget, target))
        answer = answers.get(target, _related())
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(card_contract_advice, "_freshness_states", fake_states)
    monkeypatch.setattr(sg, "analytics_query", fake_query)
    return calls


def _tests_on_disk(root: Path, *paths: str) -> None:
    for relative in paths:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def test_ok():\n    assert True\n", encoding="utf-8")


# --- the selector ---------------------------------------------------------------------


def test_ranks_by_score_then_path_and_keeps_the_best_score_per_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tests_on_disk(
        tmp_path, "tests/test_a.py", "tests/test_b.py", "tests/test_c.py", "tests/test_d.py"
    )
    calls = _fake_graph(
        monkeypatch,
        {
            ENGINE: _related(("tests/test_b.py", 40), ("tests/test_c.py", 12)),
            HELPERS: _related(("tests/test_a.py", 40), ("tests/test_c.py", 80),
                              ("tests/test_d.py", FLOOR - 1)),
        },
    )
    selection = neighbours.select_neighbour_tests(
        tmp_path, [HELPERS, ENGINE, "tests/test_x.py", "docs/a.md"], []
    )
    assert selection.tests == ("tests/test_c.py", "tests/test_a.py", "tests/test_b.py")
    assert selection.reason == ""
    assert selection.production_paths == (ENGINE, HELPERS)
    budget = card_contract_advice.TESTMAP_BUDGET
    assert calls == [("testmap", ENGINE, budget, ENGINE), ("testmap", HELPERS, budget, HELPERS)]


def test_tests_the_declared_validation_names_are_not_selected_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tests_on_disk(tmp_path, "tests/test_a.py", "tests/test_b.py", "tests/sub/test_s.py")
    _fake_graph(
        monkeypatch,
        {ENGINE: _related(("tests/test_a.py", 50), ("tests/test_b.py", 40),
                          ("tests/sub/test_s.py", 30))},
    )
    selection = neighbours.select_neighbour_tests(
        tmp_path,
        [ENGINE],
        ["python -m pytest tests/test_a.py::test_ok -q", "python -m pytest tests/sub -q"],
    )
    assert selection.tests == ("tests/test_b.py",)


def test_selection_is_capped_and_the_ceiling_is_twelve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = [f"tests/test_n{index:02d}.py" for index in range(20)]
    _tests_on_disk(tmp_path, *paths)
    _fake_graph(monkeypatch, {ENGINE: _related(*((path, 50) for path in paths))})

    capped = neighbours.select_neighbour_tests(tmp_path, [ENGINE], [], limit=3)
    assert capped.tests == tuple(paths[:3])
    assert capped.evidence()["omitted"] == 17

    ceiling = neighbours.select_neighbour_tests(tmp_path, [ENGINE], [], limit=500)
    assert len(ceiling.tests) == neighbours.MAX_NEIGHBOUR_TESTS == 12
    assert neighbours.DEFAULT_NEIGHBOUR_LIMIT <= neighbours.MAX_NEIGHBOUR_TESTS
    assert neighbours.select_neighbour_tests(tmp_path, [ENGINE], []).tests == tuple(
        paths[: neighbours.DEFAULT_NEIGHBOUR_LIMIT]
    )

    disabled = neighbours.select_neighbour_tests(tmp_path, [ENGINE], [], limit=0)
    assert disabled.tests == () and disabled.reason == "neighbour_limit_zero"


def test_selection_is_deterministic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = ["tests/test_z.py", "tests/test_m.py", "tests/test_a.py"]
    _tests_on_disk(tmp_path, *paths)
    _fake_graph(monkeypatch, {ENGINE: _related(*((path, 20) for path in paths))})
    first = neighbours.select_neighbour_tests(tmp_path, [ENGINE], [])
    assert first.tests == ("tests/test_a.py", "tests/test_m.py", "tests/test_z.py")
    assert neighbours.select_neighbour_tests(tmp_path, [ENGINE], []) == first


def test_missing_or_unquotable_test_paths_are_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tests_on_disk(tmp_path, "tests/test_here.py", "tests/test a.py")
    _fake_graph(
        monkeypatch,
        {ENGINE: _related(("tests/test_gone.py", 90), ("tests/test a.py", 80),
                          ("tests/test_here.py", 10))},
    )
    selection = neighbours.select_neighbour_tests(tmp_path, [ENGINE], [])
    assert selection.tests == ("tests/test_here.py",)


def test_unavailable_index_returns_no_tests_and_a_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_graph(monkeypatch, {}, freshness=sg.SourceGraphError("index_missing: no db"))
    selection = neighbours.select_neighbour_tests(tmp_path, [ENGINE], [])
    assert selection.tests == ()
    assert selection.reason == "index_missing"
    assert selection.evidence()["reason"] == "index_missing"


def test_engine_failure_and_stale_paths_return_no_tests_and_a_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_graph(monkeypatch, {ENGINE: RuntimeError("locked")})
    failed = neighbours.select_neighbour_tests(tmp_path, [ENGINE], [])
    assert failed.tests == ()
    assert failed.reason == "source_graph_unavailable:RuntimeError"

    _fake_graph(monkeypatch, {}, freshness={ENGINE: "stale"})
    stale = neighbours.select_neighbour_tests(tmp_path, [ENGINE], [])
    assert stale.tests == () and stale.reason == "index_stale"
    assert stale.evidence()["unavailable_paths"] == [{"path": ENGINE, "reason": "index_stale"}]


def test_no_production_change_or_no_related_test_is_a_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_graph(monkeypatch, {})
    none = neighbours.select_neighbour_tests(tmp_path, ["tests/test_a.py", "README.md"], [])
    assert none.tests == () and none.reason == "no_changed_production_python"
    assert calls == []

    unrelated = neighbours.select_neighbour_tests(tmp_path, [ENGINE], [])
    assert unrelated.tests == () and unrelated.reason == "no_related_tests"


def test_selector_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise TypeError("unexpected")

    _fake_graph(monkeypatch, {})
    monkeypatch.setattr(card_contract_advice, "_related_tests", boom)
    selection = neighbours.select_neighbour_tests(tmp_path, [ENGINE], [])
    assert selection.tests == ()
    assert selection.reason == "neighbour_selection_failed:TypeError"


def test_selector_reuses_the_contract_advice_testmap_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tests_on_disk(tmp_path, "tests/test_a.py")
    _fake_graph(monkeypatch, {ENGINE: _related(("tests/test_a.py", 50))})
    real = card_contract_advice._related_tests
    seen: list[Any] = []

    def spy(*args: Any) -> Any:
        seen.append(args)
        return real(*args)

    monkeypatch.setattr(card_contract_advice, "_related_tests", spy)
    assert neighbours.select_neighbour_tests(tmp_path, [ENGINE], []).tests == (
        "tests/test_a.py",
    )
    assert len(seen) == 1
    assert not hasattr(neighbours, "analytics_query")


# --- the run --------------------------------------------------------------------------


_CARD = {
    "allowed_writes": [ENGINE],
    "validation": ["python -m pytest tests/test_engine.py -q"],
    "validation_roles": ["generic"],
    "work_kind": "generic",
    RECEIPT_CARD_KEY: {"digest": "a"},
}
_SELECTION = neighbours.NeighbourSelection(
    tests=("tests/test_b.py", "tests/test_a.py"), reason="", limit=12,
    production_paths=(ENGINE,), related_count=2,
)
_COMMAND = "python -m pytest tests/test_b.py tests/test_a.py -q"


class _Runner:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, workspace: Any, authority: Any, route_metadata: Any) -> Any:
        self.calls.append((workspace, dict(authority), route_metadata))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _clock() -> Any:
    ticks = iter([10.0, 12.5])
    return lambda: next(ticks)


def _run(runner: _Runner, selection: Any = _SELECTION) -> dict[str, Any]:
    return neighbours.run_neighbour_tests(
        selection, _CARD, run=runner, workspace="union", route_metadata={"r": 1},
        clock=_clock(),
    )


def test_passing_neighbours_record_the_selection_command_and_duration() -> None:
    rows = [{"command": _COMMAND, "returncode": 0, "timed_out": False}]
    runner = _Runner(rows)
    evidence = _run(runner)
    assert evidence["selected"] == ["tests/test_b.py", "tests/test_a.py"]
    assert evidence["command"] == _COMMAND
    assert evidence["duration_seconds"] == 2.5
    assert evidence["validation"] == rows
    assert "reason" not in evidence
    [(workspace, authority, route)] = runner.calls
    assert workspace == "union" and route == {"r": 1}
    assert authority["validation"] == [_COMMAND]
    assert authority["allowed_writes"] == [ENGINE]
    for key in ("validation_roles", "work_kind", RECEIPT_CARD_KEY):
        assert key not in authority


def _refusal(runner: _Runner) -> dict[str, Any]:
    with pytest.raises(WorkspaceError) as excinfo:
        _run(runner)
    message = str(excinfo.value)
    assert message.startswith(neighbours.NEIGHBOUR_FAILURE_PREFIX)
    assert len(message) <= len(neighbours.NEIGHBOUR_FAILURE_PREFIX) + 4000
    return json.loads(message[len(neighbours.NEIGHBOUR_FAILURE_PREFIX):])


def test_failing_neighbour_refuses_naming_the_failed_test() -> None:
    row = {
        "command": _COMMAND,
        "returncode": 1,
        "stdout_tail": (
            "..F\nFAILED tests/test_a.py::test_contract - AssertionError: drift\n"
            "FAILED tests/test_a.py::test_contract - AssertionError: drift\n"
            "ERROR tests/test_b.py - ImportError\n1 failed, 2 passed\n"
        ),
    }
    detail = _refusal(_Runner(ValidationRunError("validation_failed:x", [row])))
    assert detail == {
        "tests": ["tests/test_b.py", "tests/test_a.py"],
        "failed": ["tests/test_a.py::test_contract", "tests/test_b.py"],
        "first_failure": "FAILED tests/test_a.py::test_contract - AssertionError: drift",
    }


def test_red_rows_or_no_rows_refuse_even_without_an_exception() -> None:
    detail = _refusal(_Runner([{"command": _COMMAND, "returncode": 0, "timed_out": True}]))
    assert detail["failed"] == [] and detail["first_failure"]
    assert _refusal(_Runner([]))["first_failure"] == "neighbour_validation_returned_no_rows"


def test_environment_refusal_is_not_a_pass() -> None:
    detail = _refusal(_Runner(WorkspaceError("validation_environment_blocked:x")))
    assert detail["first_failure"] == "validation_environment_blocked:x"


def test_empty_selection_runs_nothing_and_records_the_reason() -> None:
    runner = _Runner([])
    empty = neighbours.NeighbourSelection(tests=(), reason="index_missing", limit=12)
    evidence = _run(runner, empty)
    assert runner.calls == []
    assert evidence["selected"] == [] and evidence["reason"] == "index_missing"


# --- the accept_review lane -----------------------------------------------------------


def _accept_review_node() -> ast.FunctionDef:
    tree = ast.parse(
        Path(process_launcher_accept_review.__file__).read_text(encoding="utf-8")
    )
    return next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "accept_review"
    )


def _ancestors(root: ast.AST, target: ast.AST) -> list[ast.AST]:
    parents = {id(child): node for node in ast.walk(root) for child in ast.iter_child_nodes(node)}
    chain: list[ast.AST] = []
    node = target
    while id(node) in parents:
        node = parents[id(node)]
        chain.append(node)
    return chain


def _call_named(fn: ast.FunctionDef, name: str) -> ast.Call:
    return next(
        node for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == name
    )


def test_accept_review_imports_the_lane_inside_the_function() -> None:
    fn = _accept_review_node()
    assert any(
        isinstance(node, ast.ImportFrom)
        and any(alias.name == "accept_neighbour_tests" for alias in node.names)
        for node in ast.walk(fn)
    )


def test_a_selection_forces_the_combined_tree_and_a_failure_refuses_the_accept() -> None:
    fn = _accept_review_node()
    run_call = _call_named(fn, "run_neighbour_tests")
    chain = _ancestors(fn, run_call)
    lane = next(
        node for node in chain
        if isinstance(node, ast.If) and "combined_tree_required" in ast.unparse(node.test)
    )
    assert "neighbour_selection.tests" in ast.unparse(lane.test)
    # The run uses the accept's own runner on the union workspace ...
    keywords = {kw.arg: ast.unparse(kw.value) for kw in run_call.keywords}
    assert keywords["run"] == "_run_declared_validations"
    assert keywords["workspace"] == "union_workspace"
    # ... and its WorkspaceError lands in the handler that keeps the card in review.
    assert any(
        isinstance(node, ast.Try)
        and any(
            handler.type is not None and ast.unparse(handler.type) == "WorkspaceError"
            for handler in node.handlers
        )
        for node in chain
    )
    select_call = _call_named(fn, "select_neighbour_tests")
    assert select_call.lineno < lane.lineno


def test_accept_review_records_neighbour_evidence_on_the_combined_tree_and_gate() -> None:
    source = ast.unparse(_accept_review_node())
    assert "combined_tree['neighbour_tests'] = neighbour_tests" in source
    assert "quality_gate['neighbour_tests'] = neighbour_tests" in source
