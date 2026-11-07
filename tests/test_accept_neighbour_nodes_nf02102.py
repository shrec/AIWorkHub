"""NF-2026-02102: the neighbour lane selects impacted test nodes, failing closed to files.

A fixture repo (one src module, a 50-test file) is indexed into a real Source
Graph SQLite schema (``source_graph.SCHEMA`` in ``:memory:``) with entities and
call edges, and node selection runs through the real
``source_graph._calls_edges_and_unattributed_count``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import accept_neighbour_nodes as nodes
from aiworkhub import accept_neighbour_tests as neighbours
from aiworkhub import card_contract_advice
from aiworkhub import source_graph as sg

MOD = "src/pkg/mod.py"
MID = "src/pkg/mid.py"
TESTS = "tests/test_mod.py"
BIG = "tests/test_big.py"
MOD_LINES = [
    "CONST = 1",  # 1
    "",
    "def helper():",  # 3
    "    return CONST",  # 4
    "",
    "def other():",  # 6
    "    return 2",  # 7
    "",
    "class Box:",  # 9
    "    def size(self):",  # 10
    "        return 3",  # 11
    "",
    "def via():",  # 13
    "    return helper()",  # 14
    "",
    "def deep():",  # 16
    "    return 4",  # 17
    "",
    "def lonely():",  # 19
    "    return 5",  # 20
]
# (file, kind, name, qualname, line_start, line_end)
ENTITIES = [
    (MOD, "function", "helper", f"{MOD}::helper", 3, 4),
    (MOD, "function", "other", f"{MOD}::other", 6, 7),
    (MOD, "class", "Box", f"{MOD}::Box", 9, 11),
    (MOD, "method", "size", f"{MOD}::Box.size", 10, 11),
    (MOD, "function", "via", f"{MOD}::via", 13, 14),
    (MOD, "function", "deep", f"{MOD}::deep", 16, 17),
    (MOD, "function", "lonely", f"{MOD}::lonely", 19, 20),
    (MID, "function", "mid", f"{MID}::mid", 1, 2),
    (MID, "function", "top", f"{MID}::top", 4, 5),
    (TESTS, "class", "TestBox", f"{TESTS}::TestBox", 200, 202),
    (TESTS, "method", "test_size", f"{TESTS}::TestBox.test_size", 201, 202),
] + [
    (TESTS, "function", f"test_{i}", f"{TESTS}::test_{i}", 1 + 3 * i, 2 + 3 * i)
    for i in range(50)
]
# (caller_file, caller_qualname, callee_name, callee_qualname)
EDGES = [
    (TESTS, f"{TESTS}::test_1", "other", f"{MOD}::other"),
    (TESTS, f"{TESTS}::test_2", "other", f"{MOD}::other"),
    (TESTS, f"{TESTS}::TestBox.test_size", "size", f"{MOD}::Box.size"),
    (MOD, f"{MOD}::via", "helper", f"{MOD}::helper"),
    (TESTS, f"{TESTS}::test_7", "via", f"{MOD}::via"),
    (MID, f"{MID}::mid", "deep", f"{MOD}::deep"),
    (MID, f"{MID}::top", "mid", f"{MID}::mid"),
    (TESTS, f"{TESTS}::test_9", "top", f"{MID}::top"),
]


def _db(entities: list[tuple[Any, ...]] = ENTITIES, edges: list[tuple[Any, ...]] = EDGES):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(sg.SCHEMA)
    conn.executemany(
        "INSERT INTO entities (file_path, kind, name, qualname, line_start, line_end, "
        "evidence_label, extractor, confidence, source_hash, build_revision) "
        "VALUES (?, ?, ?, ?, ?, ?, 'ast', 'python', 1.0, 'h', 'r')",
        entities,
    )
    conn.executemany(
        "INSERT INTO edges (file_path, kind, src_qualname, dst_name, dst_qualname, line, "
        "evidence_label, extractor, confidence, source_hash, build_revision) "
        "VALUES (?, 'calls', ?, ?, ?, 1, 'ast', 'python', 1.0, 'h', 'r')",
        edges,
    )
    return conn


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _repo(tmp_path: Path, **changes: str) -> tuple[Path, Path]:
    """Canonical repo and a candidate copy whose 1-based lines ``l<n>`` are replaced."""
    root, candidate = tmp_path / "repo", tmp_path / "candidate"
    _write(root / MOD, "\n".join(MOD_LINES) + "\n")
    tests = "".join(f"def test_{i}():\n    assert True\n\n" for i in range(50))
    for side in (root, candidate):  # the candidate carries the test files unchanged
        _write(side / TESTS, tests + "class TestBox:\n    def test_size(self):\n        pass\n")
        _write(side / BIG, "def test_big():\n    assert True\n")
    lines = list(MOD_LINES)
    for key, text in changes.items():
        lines[int(key[1:]) - 1] = text
    _write(candidate / MOD, "\n".join(lines) + "\n")
    return root, candidate


def _select(tmp_path: Path, conn: sqlite3.Connection, **changes: str) -> nodes.NodeSelectionResult:
    root, candidate = _repo(tmp_path, **changes)
    return nodes.select_nodes_for_path(conn, MOD, root, candidate)


# --- per-path node selection against the real schema -----------------------------------


def test_a_body_change_selects_exactly_the_two_calling_tests(tmp_path: Path) -> None:
    result = _select(tmp_path, _db(), l7="    return 22")
    assert result == nodes.NodeSelectionResult(
        nodes=(f"{TESTS}::test_1", f"{TESTS}::test_2")
    )


def test_method_change_selects_the_test_method_node(tmp_path: Path) -> None:
    result = _select(tmp_path, _db(), l11="        return 33")
    assert result.nodes == (f"{TESTS}::TestBox::test_size",)
    assert not result.fallback


def test_b_module_level_constant_change_falls_back(tmp_path: Path) -> None:
    result = _select(tmp_path, _db(), l1="CONST = 2")
    assert (result.fallback, result.reason, result.nodes) == (True, "module_level", ())


def test_class_level_and_function_edge_insertion_fall_back(tmp_path: Path) -> None:
    assert _select(tmp_path, _db(), l9="class Box(object):").reason == "module_level"
    assert nodes.changed_old_ranges(["a", "b", "c"], ["a", "x", "b", "c"]) == [(1, 2)]
    assert nodes.changed_old_ranges(["a"], ["a", "z"]) == [(1, 2)]


def test_d_helper_reached_via_a_production_caller_selects_its_tests(tmp_path: Path) -> None:
    result = _select(tmp_path, _db(), l4="    return CONST + 1")
    assert result.nodes == (f"{TESTS}::test_7",)


def test_e_no_test_caller_within_depth_two_falls_back(tmp_path: Path) -> None:
    deep = _select(tmp_path / "deep", _db(), l17="    return 44")
    assert (deep.fallback, deep.reason) == (True, "depth_exceeded")
    lonely = _select(tmp_path / "lonely", _db(), l20="    return 55")
    assert (lonely.fallback, lonely.reason) == (True, "no_test_callers")


def test_unattributed_same_name_caller_falls_back(tmp_path: Path) -> None:
    entities = ENTITIES + [("src/pkg/dup.py", "function", "other", "src/pkg/dup.py::other", 1, 2)]
    edges = EDGES + [("src/pkg/x.py", "src/pkg/x.py::caller", "other", None)]
    result = _select(tmp_path, _db(entities, edges), l7="    return 22")
    assert (result.fallback, result.reason) == (True, "unattributed")


def test_c_unavailable_index_or_unreadable_candidate_falls_back(tmp_path: Path) -> None:
    root, candidate = _repo(tmp_path, l7="    return 22")

    def broken(_root: Path) -> sqlite3.Connection:
        raise sg.RepositoryUnresolvedError("no index")

    found, reasons = nodes.select_nodes(
        root, candidate, [MOD], {MOD: "fresh"}, connect=broken
    )
    assert found == ()
    assert reasons == ({"path": MOD, "reason": "index_error:RepositoryUnresolvedError"},)
    stale = nodes.select_nodes(root, candidate, [MOD], {MOD: "stale"}, connect=lambda _r: _db())
    assert stale == ((), ({"path": MOD, "reason": "stale"},))
    missing = nodes.select_nodes_for_path(_db(), MOD, root, tmp_path / "nowhere")
    assert missing.reason == "candidate_unreadable"


def test_f_selection_is_identical_when_computed_twice(tmp_path: Path) -> None:
    root, candidate = _repo(tmp_path, l7="    return 22", l11="        return 33")
    first = nodes.select_nodes(root, candidate, [MOD], {MOD: "fresh"}, connect=lambda _r: _db())
    again = nodes.select_nodes(root, candidate, [MOD], {MOD: "fresh"}, connect=lambda _r: _db())
    assert first == again
    assert first[0] == (
        f"{TESTS}::TestBox::test_size", f"{TESTS}::test_1", f"{TESTS}::test_2"
    )


# --- wired into select_neighbour_tests ---------------------------------------------------


def _fake_testmap(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_states(root: Path, paths: Any) -> dict[str, str]:
        return {path: "fresh" for path in paths}

    def fake_query(root: Path, mode: str, query: str, budget: int = 64, **_: Any) -> Any:
        rows = [{"file_path": path, "score": 90, "reasons": []} for path in (BIG, TESTS)]
        return {"scope": "query_matches", "related_tests": rows, "truncated": False}

    monkeypatch.setattr(card_contract_advice, "_freshness_states", fake_states)
    monkeypatch.setattr(sg, "analytics_query", fake_query)
    monkeypatch.setattr(nodes, "_connect", lambda _root: _db())


def test_select_neighbour_tests_with_candidate_root_returns_node_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_testmap(monkeypatch)
    root, candidate = _repo(tmp_path, l7="    return 22")
    files = neighbours.select_neighbour_tests(root, [MOD], [])
    assert files.tests == (BIG, TESTS)
    assert "selection_mode" not in files.evidence()  # no candidate_root: unchanged
    selection = neighbours.select_neighbour_tests(root, [MOD], [], candidate_root=candidate)
    assert selection.tests == (f"{TESTS}::test_1", f"{TESTS}::test_2")
    evidence = selection.evidence()
    assert (evidence["selection_mode"], evidence["node_level"], evidence["node_count"]) == (
        "node", True, 2
    )
    assert evidence["fallback_reasons"] == []
    assert neighbours.select_neighbour_tests(root, [MOD], [], candidate_root=candidate) == selection
    named = neighbours.select_neighbour_tests(
        root, [MOD], [f"python -m pytest -q {TESTS}"], candidate_root=candidate
    )
    assert named.tests == ()  # the declared validation already runs those nodes' file


def test_select_neighbour_tests_keeps_file_selection_when_a_path_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_testmap(monkeypatch)
    root, candidate = _repo(tmp_path, l1="CONST = 2", l7="    return 22")
    files = neighbours.select_neighbour_tests(root, [MOD], [])
    selection = neighbours.select_neighbour_tests(root, [MOD], [], candidate_root=candidate)
    assert selection.tests == files.tests == (BIG, TESTS)
    evidence = selection.evidence()
    assert evidence["selection_mode"] == "file_fallback"
    assert evidence["node_level"] is False
    assert evidence["fallback_reasons"] == [{"path": MOD, "reason": "module_level"}]
    assert {k: v for k, v in evidence.items() if k not in {
        "selection_mode", "node_level", "node_count", "fallback_reasons"
    }} == files.evidence()


def test_f_run_finalization_neighbours_forwards_candidate_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[Any] = []

    def fake_select(*args: Any, candidate_root: Any = None, **_: Any) -> Any:
        seen.append(candidate_root)
        return neighbours.NeighbourSelection(tests=(), reason="none", limit=12)

    monkeypatch.setattr(neighbours, "select_neighbour_tests", fake_select)
    result = neighbours.run_finalization_neighbours(
        tmp_path, [MOD], {}, [], run=lambda *_: [], workspace=None,
        route_metadata={}, candidate_root=tmp_path / "snap",
    )
    assert result is None
    assert seen == [tmp_path / "snap"]


def test_g_invariant_tests_include_the_os_dependency_boundary() -> None:
    assert "tests/test_os_dependency_boundary.py" in neighbours.INVARIANT_TESTS
    assert neighbours.INVARIANT_TESTS[0] == "tests/test_module_size_ratchet.py"


# --- rework: fail-open cases found in review ---------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"l7": "    return 1\n\nX = g()"},  # a new top-level statement
        {"l7": "    return 1\ndef h():\n    return 9"},  # a new top-level def
        {"l7": "    return 1\n@deco"},  # a new decorator
        {"l11": "        return 1\n    def extra(self):\n        return 0"},  # a new method
    ],
)
def test_f1_new_side_lines_leaving_the_function_fall_back(
    tmp_path: Path, changes: dict[str, str]
) -> None:
    result = _select(tmp_path, _db(), **changes)
    assert (result.fallback, result.reason, result.nodes) == (True, "module_level", ())


def test_f1_pure_in_body_replacement_still_selects_nodes(tmp_path: Path) -> None:
    result = _select(tmp_path, _db(), l7="    y = 1\n# note\n\n    return y")
    assert result == nodes.NodeSelectionResult(
        nodes=(f"{TESTS}::test_1", f"{TESTS}::test_2")
    )


def test_f2_production_caller_without_callers_falls_back(tmp_path: Path) -> None:
    entities = ENTITIES + [(MID, "function", "orphan", f"{MID}::orphan", 7, 8)]
    edges = EDGES + [(MID, f"{MID}::orphan", "other", f"{MOD}::other")]
    result = _select(tmp_path, _db(entities, edges), l7="    return 22")
    assert (result.fallback, result.reason, result.nodes) == (True, "caller_without_callers", ())


def test_f3_node_whose_test_file_the_candidate_changed_falls_back_to_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_testmap(monkeypatch)
    root, candidate = _repo(tmp_path, l7="    return 22")
    expected = [
        {"path": f"{TESTS}::test_1", "reason": "test_file_changed_in_candidate"},
        {"path": f"{TESTS}::test_2", "reason": "test_file_changed_in_candidate"},
    ]
    _write(candidate / TESTS, "def test_renamed():\n    assert True\n")
    changed = neighbours.select_neighbour_tests(root, [MOD], [], candidate_root=candidate)
    assert changed.tests == (BIG, TESTS)
    assert changed.evidence()["selection_mode"] == "file_fallback"
    assert changed.evidence()["fallback_reasons"] == expected
    (candidate / TESTS).unlink()
    removed = neighbours.select_neighbour_tests(root, [MOD], [], candidate_root=candidate)
    assert removed.tests == (BIG, TESTS)
    assert removed.evidence()["fallback_reasons"] == expected


def test_f4_form_feed_inside_a_function_keeps_lines_aligned(tmp_path: Path) -> None:
    root, candidate = _repo(tmp_path)
    for side, seventh in ((root, "    return 2"), (candidate, "    return 22")):
        lines = list(MOD_LINES)
        lines[3], lines[6] = "    return CONST  # \x0c page", seventh
        _write(side / MOD, "\n".join(lines) + "\n")
    result = nodes.select_nodes_for_path(_db(), MOD, root, candidate)
    assert result == nodes.NodeSelectionResult(
        nodes=(f"{TESTS}::test_1", f"{TESTS}::test_2")
    )


def _select_lines(tmp_path: Path, lines: list[str]) -> nodes.NodeSelectionResult:
    root, candidate = _repo(tmp_path)
    _write(candidate / MOD, "\n".join(lines) + "\n")
    return nodes.select_nodes_for_path(_db(), MOD, root, candidate)


def test_def_line_deleted_falls_back(tmp_path: Path) -> None:
    lines = list(MOD_LINES)
    del lines[5]  # "def other():": its body now hangs off the previous statement
    result = _select_lines(tmp_path, lines)
    assert (result.fallback, result.reason, result.nodes) == (True, "def_line_changed", ())


def test_def_line_replaced_by_a_deeper_indented_line_falls_back(tmp_path: Path) -> None:
    result = _select(tmp_path, _db(), l6="    x = 0")
    assert (result.fallback, result.reason, result.nodes) == (True, "def_line_changed", ())


def test_body_line_inserted_right_after_the_def_line_keeps_selection(tmp_path: Path) -> None:
    lines = list(MOD_LINES)
    lines.insert(6, "    y = 0")  # between "def other():" and "    return 2"
    result = _select_lines(tmp_path, lines)
    assert result == nodes.NodeSelectionResult(
        nodes=(f"{TESTS}::test_1", f"{TESTS}::test_2")
    )
    old, new = list(MOD_LINES), lines
    assert nodes.changed_hunks(old, new) == [((6, 7), ("    y = 0",), None)]
