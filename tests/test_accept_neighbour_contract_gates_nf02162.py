"""NF-2026-02162: a changed MCP tool-surface module pins the schema-freeze contract gates.

``tests/test_mcp_contract_gates.py`` drives the standalone ``tests/mcp_*.py`` freeze
scripts as subprocesses, so no call-graph lane links ``server.py`` to them: dfa2ea6d
changed two tool input schemas and was accepted green while that gate went red. The
neighbour selector therefore appends the gate file whenever a tool-surface module is
among the changed paths -- outside the neighbour cap, with a ``contract_gate:`` reason.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aiworkhub import accept_neighbour_tests as neighbours
from aiworkhub import card_contract_advice
from aiworkhub import source_graph as sg

SERVER = "src/aiworkhub/server.py"
METADATA = "src/aiworkhub/mcp_tool_metadata.py"
ENGINE = "src/tinypkg/engine.py"
GATE = "tests/test_mcp_contract_gates.py"


def _related(*rows: tuple[str, int]) -> dict[str, Any]:
    return {
        "scope": "query_matches",
        "related_tests": [
            {"file_path": path, "score": score, "reasons": []} for path, score in rows
        ],
        "truncated": False,
    }


def _fake_graph(
    monkeypatch: pytest.MonkeyPatch, answers: dict[str, Any], *, freshness: Any = None
) -> None:
    def fake_states(root: Path, paths: Any) -> dict[str, str]:
        if isinstance(freshness, BaseException):
            raise freshness
        return {path: "fresh" for path in paths}

    def fake_query(
        root: Path,
        mode: str,
        query: str,
        budget: int = 64,
        *,
        target: str | None = None,
        cursor: str | None = None,
    ) -> Any:
        return answers.get(target, _related())

    monkeypatch.setattr(card_contract_advice, "_freshness_states", fake_states)
    monkeypatch.setattr(sg, "analytics_query", fake_query)


def _tests_on_disk(root: Path, *paths: str) -> None:
    for relative in paths:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def test_ok():\n    assert True\n", encoding="utf-8")


def test_changed_server_pins_the_contract_gate_with_a_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tests_on_disk(tmp_path, GATE, "tests/test_a.py")
    _fake_graph(monkeypatch, {SERVER: _related(("tests/test_a.py", 50))})
    selection = neighbours.select_neighbour_tests(tmp_path, [SERVER], [])
    assert selection.tests == ("tests/test_a.py", GATE)
    assert selection.contract_gate == (GATE,)
    assert selection.contract_gate_reason == "contract_gate:server.py"
    evidence = selection.evidence()
    assert evidence["contract_gate"] == [GATE]
    assert evidence["contract_gate_reason"] == "contract_gate:server.py"
    assert evidence["omitted"] == 0


def test_every_tool_surface_module_is_a_trigger_and_all_are_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert set(neighbours.CONTRACT_GATE_MODULES) == {SERVER, METADATA}
    _tests_on_disk(tmp_path, GATE)
    _fake_graph(monkeypatch, {})
    only_metadata = neighbours.select_neighbour_tests(tmp_path, [METADATA], [])
    assert only_metadata.contract_gate_reason == "contract_gate:mcp_tool_metadata.py"
    both = neighbours.select_neighbour_tests(tmp_path, [SERVER, METADATA], [])
    assert both.contract_gate_reason == "contract_gate:mcp_tool_metadata.py,server.py"
    assert both.tests.count(GATE) == 1


def test_unrelated_change_leaves_the_selection_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tests_on_disk(tmp_path, GATE, "tests/test_a.py", "tests/test_b.py")
    _fake_graph(monkeypatch, {ENGINE: _related(("tests/test_a.py", 50), ("tests/test_b.py", 20))})
    selection = neighbours.select_neighbour_tests(tmp_path, [ENGINE, "docs/a.md"], [])
    assert selection.tests == ("tests/test_a.py", "tests/test_b.py")
    assert selection.contract_gate == () and selection.contract_gate_reason == ""
    evidence = selection.evidence()
    assert "contract_gate" not in evidence and "contract_gate_reason" not in evidence
    # A test file named server.py is not a tool-surface module either.
    other = neighbours.select_neighbour_tests(tmp_path, ["tests/server.py"], [])
    assert other.contract_gate == ()


def test_pinned_gate_survives_a_selection_already_at_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ranked = [f"tests/test_n{index:02d}.py" for index in range(20)]
    _tests_on_disk(tmp_path, GATE, *ranked)
    _fake_graph(monkeypatch, {SERVER: _related(*((path, 50) for path in ranked))})
    selection = neighbours.select_neighbour_tests(tmp_path, [SERVER], [], limit=500)
    cap = neighbours.MAX_NEIGHBOUR_TESTS
    assert selection.tests == (*ranked[:cap], GATE)
    assert len(selection.tests) == cap + 1
    assert selection.evidence()["omitted"] == len(ranked) - cap
    small = neighbours.select_neighbour_tests(tmp_path, [SERVER], [], limit=3)
    assert small.tests == (*ranked[:3], GATE)


def test_a_ranked_gate_folds_into_the_pinned_whole_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tests_on_disk(tmp_path, GATE, "tests/test_a.py")
    _fake_graph(monkeypatch, {SERVER: _related((GATE, 90), ("tests/test_a.py", 50))})
    selection = neighbours.select_neighbour_tests(tmp_path, [SERVER], [])
    assert selection.tests == ("tests/test_a.py", GATE)
    assert selection.evidence()["omitted"] == 0


def test_gate_is_pinned_when_the_index_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tests_on_disk(tmp_path, GATE)
    _fake_graph(monkeypatch, {}, freshness=sg.SourceGraphError("index_missing: no db"))
    selection = neighbours.select_neighbour_tests(tmp_path, [SERVER], [])
    assert selection.reason == "index_missing"
    assert GATE in selection.tests
    assert selection.contract_gate_reason == "contract_gate:server.py"


def test_gate_the_declared_validation_names_or_a_disabled_lane_adds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tests_on_disk(tmp_path, GATE)
    _fake_graph(monkeypatch, {})
    named = neighbours.select_neighbour_tests(
        tmp_path, [SERVER], [f"python -m pytest {GATE} -q"]
    )
    assert GATE not in named.tests and named.contract_gate_reason == ""
    disabled = neighbours.select_neighbour_tests(tmp_path, [SERVER], [], limit=0)
    assert disabled.tests == () and disabled.contract_gate == ()


def test_a_missing_gate_file_is_named_not_silently_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_graph(monkeypatch, {})
    selection = neighbours.select_neighbour_tests(tmp_path, [SERVER], [])
    assert GATE not in selection.tests
    assert selection.contract_gate_reason == f"contract_gate_missing:{GATE}"
    assert selection.evidence()["contract_gate_reason"] == selection.contract_gate_reason


def test_a_failing_pin_is_recorded_and_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tests_on_disk(tmp_path, GATE)
    _fake_graph(monkeypatch, {})

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("denied")

    monkeypatch.setattr(neighbours, "_with_contract_gates", boom)
    selection = neighbours.select_neighbour_tests(tmp_path, [SERVER], [])
    assert selection.contract_gate_reason == "contract_gate_failed:OSError"
