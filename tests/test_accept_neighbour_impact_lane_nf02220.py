"""NF-2026-02220 part B: the accept neighbour lane uses the test-impact selector.

When the recorded map (``aiworkhub.test_impact_select``) is fresh for every changed
path, the selector's tests -- plus the declared invariant tests -- are the neighbour
selection and the evidence records ``lane: impact_map``. Any unmapped or stale path,
an unusable map or candidate tree, or a selector that raises falls back to the
structural testmap selection unchanged, recorded as ``lane: structural`` with the
reason. The selection is never narrower than the selector's result together with the
card's own declared runs.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import accept_neighbour_tests as neighbours
from aiworkhub import card_contract_advice
from aiworkhub import source_graph as sg
from aiworkhub import test_impact_select as tis

MOD = "src/pkg/mod.py"
TEST_MOD = "tests/test_mod.py"
TEST_OTHER = "tests/test_other.py"
RATCHET = "tests/test_module_size_ratchet.py"
NODE_F = f"{TEST_MOD}::test_f"
_FILES = {
    ".gitignore": ".aiworkhub/\n__pycache__/\n",
    "src/pkg/__init__.py": "",
    MOD: "def f():\n    return 1\n",
    TEST_MOD: "from pkg import mod\n\n\ndef test_f():\n    assert mod.f() == 1\n",
    TEST_OTHER: "def test_o():\n    assert True\n",
    RATCHET: "def test_r():\n    assert True\n",
}


def _git(repo: Path, *args: str) -> str:
    argv = [
        "git", "-c", "user.name=impact", "-c", "user.email=impact@example.invalid",
        "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args,
    ]
    return subprocess.run(argv, cwd=repo, check=True, capture_output=True, text=True).stdout


def _map_payload(root: Path) -> dict[str, Any]:
    objects = [f"{MOD}::f", NODE_F, f"{TEST_OTHER}::test_o", f"{RATCHET}::test_r"]
    rows = {
        NODE_F: (TEST_MOD, [0, 1]),
        f"{TEST_OTHER}::test_o": (TEST_OTHER, [2]),
        f"{RATCHET}::test_r": (RATCHET, [3]),
    }
    return {
        "schema_id": tis.SCHEMA_ID,
        "objects": objects,
        "tests": {
            nodeid: {"file": file, "code": code, "reads": [], "children": [], "incomplete": False}
            for nodeid, (file, code) in rows.items()
        },
        "digests": {
            path: tis.content_digest((root / path).read_bytes())
            for path in (MOD, TEST_MOD, TEST_OTHER, RATCHET)
        },
        "import_time": [],
        "import_reads": {},
    }


def _write_map(root: Path, payload: Any) -> None:
    target = tis.map_directory(root) / tis.MAP_FILE_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")


def _make_tree(path: Path, *, repository: bool) -> Path:
    path.mkdir()
    for relative, text in _FILES.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
    if repository:
        _git(path, "init", "-q", "--template=")
        _git(path, "add", "--", *sorted(_FILES))
        _git(path, "commit", "-q", "-m", "fixture")
    return path


@pytest.fixture
def trees(tmp_path: Path) -> tuple[Path, Path]:
    """The canonical tree with a fresh map, and a candidate repository that edits ``f``.

    The candidate is its own repository holding the same committed files (a local
    ``git clone`` is refused in the validation sandbox); the lane reads only files and
    the map under the canonical root, and runs the selector's git diff in the candidate.
    """
    root = _make_tree(tmp_path / "canonical", repository=False)
    _write_map(root, _map_payload(root))
    candidate = _make_tree(tmp_path / "candidate", repository=True)
    (candidate / MOD).write_bytes(b"def f():\n    return int('1')\n")
    return root, candidate


@pytest.fixture(autouse=True)
def _structural_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """The structural testmap lane relates TEST_OTHER (not TEST_MOD) to MOD."""

    def fake_states(root: Path, paths: Any) -> dict[str, str]:
        return {path: "fresh" for path in paths}

    def fake_query(
        root: Path, mode: str, query: str, budget: int = 64, *,
        target: str | None = None, cursor: str | None = None,
    ) -> Any:
        rows = [{"file_path": TEST_OTHER, "score": 50, "reasons": []}] if target == MOD else []
        return {"scope": "query_matches", "related_tests": rows, "truncated": False}

    monkeypatch.setattr(card_contract_advice, "_freshness_states", fake_states)
    monkeypatch.setattr(sg, "analytics_query", fake_query)


def _select(root: Path, candidate: Path | None, changed: Any = (MOD,), declared: Any = ()) -> Any:
    return neighbours.select_neighbour_tests(root, list(changed), list(declared), candidate_root=candidate)


def _structural_today(
    monkeypatch: pytest.MonkeyPatch, root: Path, candidate: Path | None, **kwargs: Any
) -> Any:
    """The structural selection with the impact lane switched off, as before NF-2026-02220."""
    with monkeypatch.context() as patch:
        patch.setattr(neighbours, "_impact_lane", lambda *_a, **_k: (None, "off"))
        return _select(root, candidate, **kwargs)


def _assert_structural(
    monkeypatch: pytest.MonkeyPatch, selection: Any, reason: str,
    root: Path, candidate: Path | None, **kwargs: Any,
) -> None:
    assert selection.lane == neighbours.LANE_STRUCTURAL
    assert selection.lane_reason == reason
    today = _structural_today(monkeypatch, root, candidate, **kwargs)
    assert replace(selection, lane_reason="") == replace(today, lane_reason="")
    evidence, before = selection.evidence(), today.evidence()
    assert evidence.pop("lane_reason") == reason and before.pop("lane_reason") == "off"
    assert evidence == before and evidence["lane"] == "structural"


# --- the impact lane decides ----------------------------------------------------------


def test_fresh_map_selects_the_selector_tests_plus_invariants(trees: tuple[Path, Path]) -> None:
    root, candidate = trees
    selection = _select(root, candidate)
    assert selection.tests == (NODE_F, RATCHET)
    assert selection.invariant == (RATCHET,)
    assert selection.lane == neighbours.LANE_IMPACT_MAP
    assert selection.lane_reason == "impact_map_fresh:1"
    evidence = selection.evidence()
    assert evidence["lane"] == "impact_map" and evidence["lane_reason"] == "impact_map_fresh:1"
    assert evidence["selected"] == [NODE_F, RATCHET]
    assert evidence["omitted"] == 0 and "reason" not in evidence
    # The structural lane would have run TEST_OTHER: the map proves it unaffected.
    assert TEST_OTHER not in selection.tests


def test_selection_is_never_narrower_than_selector_and_declared_runs(
    trees: tuple[Path, Path],
) -> None:
    root, candidate = trees
    (candidate / "tests/test_new.py").write_text("def test_n():\n    assert True\n", encoding="utf-8")
    changed = (MOD, "tests/test_new.py")
    map_path = tis.map_directory(root) / tis.MAP_FILE_NAME
    result = tis.select_tests(candidate, "HEAD", paths=list(changed), map_path=map_path)
    assert set(result.selected) == {NODE_F, "tests/test_new.py"}
    declared = ["python -m pytest -q tests/test_new.py"]
    selection = _select(root, candidate, changed, declared)
    assert selection.lane == neighbours.LANE_IMPACT_MAP
    assert selection.tests == (NODE_F, RATCHET)
    whole = neighbours._pytest_whole_files(declared)
    for entry in result.selected:
        file = entry.split("::", 1)[0]
        assert entry in selection.tests or file in selection.tests or (
            card_contract_advice._covers(whole, file)
        )
    # Without the declared run the new test file is a neighbour itself.
    alone = _select(root, candidate, changed)
    assert alone.tests == (NODE_F, "tests/test_new.py", RATCHET)


@pytest.mark.parametrize(
    "command",
    [
        "python -m pytest -q tests/test_mod.py -k other",
        "python -m pytest -q tests/test_mod.py --deselect tests/test_mod.py::test_f",
        "python -m pytest -q tests/test_mod.py::test_g",
        "python -m ruff check tests/test_mod.py",
    ],
)
def test_a_declared_run_that_may_skip_the_test_does_not_excuse_it(
    trees: tuple[Path, Path], command: str
) -> None:
    root, candidate = trees
    selection = _select(root, candidate, declared=[command])
    assert selection.lane == neighbours.LANE_IMPACT_MAP
    assert NODE_F in selection.tests


@pytest.mark.parametrize("option", ["--co", "--collect-only", "--setup-only", "--setup-plan"])
def test_a_declared_run_that_executes_no_test_does_not_excuse_it(
    trees: tuple[Path, Path], option: str
) -> None:
    root, candidate = trees
    selection = _select(root, candidate, declared=[f"python -m pytest -q tests/test_mod.py {option}"])
    assert selection.lane == neighbours.LANE_IMPACT_MAP
    assert NODE_F in selection.tests


@pytest.mark.parametrize(
    "option",
    ["--fixtures", "--fixtures-per-test", "--markers", "-h", "--help", "--version", "--collectonly"],
)
def test_a_declared_informational_run_does_not_excuse_an_impacted_test(
    trees: tuple[Path, Path], option: str
) -> None:
    root, candidate = trees
    command = f"python -m pytest -q tests/test_mod.py {option}"
    assert neighbours._pytest_whole_files([command]) == []
    selection = _select(root, candidate, declared=[command])
    assert selection.lane == neighbours.LANE_IMPACT_MAP
    assert NODE_F in selection.tests


def test_a_declared_whole_file_or_directory_run_covers_the_selected_test(
    trees: tuple[Path, Path],
) -> None:
    root, candidate = trees
    for command in ("python -m pytest -q tests/test_mod.py", "python -m pytest -n 4 tests -q"):
        selection = _select(root, candidate, declared=[command])
        assert selection.lane == neighbours.LANE_IMPACT_MAP
        assert NODE_F not in selection.tests


def test_unquotable_node_ids_fold_and_a_whole_file_absorbs_its_nodes(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, candidate = trees
    picked = {
        f"{TEST_MOD}::test_f[a b]": ("executes:x",),
        f"{TEST_OTHER}::test_o": ("executes:y",),
        TEST_OTHER: ("changed_test",),
        f"{RATCHET}::test_r": ("executes:z",),
    }
    monkeypatch.setattr(
        neighbours._impact, "select_tests",
        lambda *_a, **_k: tis.Selection(False, (), picked, (MOD,)),
    )
    selection = _select(root, candidate)
    assert selection.lane == neighbours.LANE_IMPACT_MAP
    assert selection.tests == (TEST_MOD, RATCHET, TEST_OTHER)


def test_an_over_long_argv_folds_to_files_then_falls_back(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, candidate = trees
    picked = {NODE_F: ("a",), f"{TEST_OTHER}::test_o": ("b",)}
    monkeypatch.setattr(
        neighbours._impact, "select_tests",
        lambda *_a, **_k: tis.Selection(False, (), picked, (MOD,)),
    )
    monkeypatch.setattr(neighbours, "MAX_IMPACT_COMMAND_CHARS", 40)
    folded = _select(root, candidate)
    assert folded.lane == neighbours.LANE_IMPACT_MAP
    assert folded.tests == (TEST_MOD, TEST_OTHER, RATCHET)
    monkeypatch.setattr(neighbours, "MAX_IMPACT_COMMAND_CHARS", 30)
    _assert_structural(
        monkeypatch, _select(root, candidate), "impact_map_selection_too_wide:2", root, candidate
    )


# --- the structural lane decides, unchanged ---------------------------------------------


def test_a_stale_changed_path_falls_back_unchanged(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, candidate = trees
    (root / MOD).write_bytes(b"def f():\n    return 2\n")  # canonical moved after recording
    selection = _select(root, candidate)
    assert selection.tests == (TEST_OTHER, RATCHET)
    _assert_structural(monkeypatch, selection, f"impact_map_stale:{MOD}", root, candidate)


@pytest.mark.parametrize("extra", ["docs/notes.md", "src/pkg/new.py"])
def test_an_unmapped_changed_path_falls_back_unchanged(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, extra: str
) -> None:
    root, candidate = trees
    changed = (MOD, extra)
    selection = _select(root, candidate, changed)
    _assert_structural(
        monkeypatch, selection, f"impact_map_unmapped:{extra}", root, candidate, changed=changed
    )


def test_a_raising_selector_falls_back_unchanged(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, candidate = trees

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("index locked")

    monkeypatch.setattr(neighbours._impact, "select_tests", boom)
    selection = _select(root, candidate)
    _assert_structural(monkeypatch, selection, "impact_map_failed:RuntimeError", root, candidate)


def test_a_full_suite_answer_falls_back_unchanged(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, candidate = trees
    answer = tis.Selection(True, ("global_invalidator:pyproject.toml",), {}, (MOD,))
    monkeypatch.setattr(neighbours._impact, "select_tests", lambda *_a, **_k: answer)
    reason = "impact_map_full_suite:global_invalidator:pyproject.toml"
    _assert_structural(monkeypatch, _select(root, candidate), reason, root, candidate)


def test_a_missing_or_foreign_map_falls_back_unchanged(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, candidate = trees
    _write_map(root, {"schema_id": "aiworkhub.test_impact.map.v0"})
    reason = "impact_map_unavailable:map_schema_mismatch"
    _assert_structural(monkeypatch, _select(root, candidate), reason, root, candidate)
    (tis.map_directory(root) / tis.MAP_FILE_NAME).unlink()
    reason = "impact_map_unavailable:map_missing"
    _assert_structural(monkeypatch, _select(root, candidate), reason, root, candidate)


def test_a_diff_that_misses_a_changed_path_falls_back_unchanged(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, candidate = trees
    (candidate / MOD).write_bytes(_FILES[MOD].encode("utf-8"))  # the claimed change is not there
    reason = f"impact_map_diff_missing:{MOD}"
    _assert_structural(monkeypatch, _select(root, candidate), reason, root, candidate)


def test_an_incomplete_candidate_tree_falls_back_unchanged(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, candidate = trees
    (candidate / TEST_OTHER).unlink()
    reason = f"impact_map_candidate_tree_incomplete:{TEST_OTHER}"
    _assert_structural(monkeypatch, _select(root, candidate), reason, root, candidate)


def test_an_unrunnable_selected_test_falls_back_unchanged(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, candidate = trees
    picked = {NODE_F: ("a",), "tests/test_gone.py": ("b",)}
    monkeypatch.setattr(
        neighbours._impact, "select_tests",
        lambda *_a, **_k: tis.Selection(False, (), picked, (MOD,)),
    )
    reason = "impact_map_unrunnable:tests/test_gone.py"
    _assert_structural(monkeypatch, _select(root, candidate), reason, root, candidate)


def test_no_candidate_tree_or_disabled_lane_is_structural_with_a_reason(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _candidate = trees
    reason = "impact_map_skipped:no_candidate_tree"
    _assert_structural(monkeypatch, _select(root, None), reason, root, None)
    disabled = neighbours.select_neighbour_tests(root, [MOD], [], limit=0, candidate_root=root)
    assert disabled.tests == () and disabled.lane == neighbours.LANE_STRUCTURAL
    assert disabled.lane_reason == "impact_map_skipped:neighbour_limit_zero"
    docs_only = _select(root, root, changed=("README.md",))
    assert docs_only.reason == "no_changed_production_python"
    assert docs_only.lane_reason == "impact_map_skipped:no_changed_production_python"


def test_finalization_records_the_deciding_lane(trees: tuple[Path, Path]) -> None:
    root, candidate = trees
    rows = [{"command": "x", "returncode": 0, "timed_out": False}]
    evidence = neighbours.run_finalization_neighbours(
        root, [MOD], {"allowed_writes": [MOD], "validation": []}, [],
        run=lambda *_a: rows, workspace="snap", route_metadata={},
        candidate_root=candidate,
    )
    assert evidence is not None and evidence["passed"] is True
    assert evidence["lane"] == "impact_map" and evidence["selected"] == [NODE_F, RATCHET]


# --- a map stale for a test fails open ---------------------------------------------------


@pytest.mark.parametrize("case", ["test_missing_from_map", "test_changed_since_recording"])
def test_a_test_the_map_is_not_fresh_for_stays_selected(
    trees: tuple[Path, Path], case: str
) -> None:
    root, candidate = trees
    if case == "test_missing_from_map":
        stale = "tests/test_added.py"
        for tree in (root, candidate):  # new in canonical after recording, so not in the map
            (tree / stale).write_text("def test_a():\n    assert True\n", encoding="utf-8")
    else:
        stale = TEST_OTHER
        (root / stale).write_text("def test_o():\n    assert 1 == 1\n", encoding="utf-8")
    selection = _select(root, candidate)
    assert selection.lane == neighbours.LANE_IMPACT_MAP
    assert selection.lane_reason == "impact_map_fresh:1;map_stale_tests_kept:1"
    assert stale in selection.tests
    assert NODE_F in selection.tests and RATCHET in selection.tests


# --- a recorded dependency the map is stale for hands the decision back -------------------


def test_a_stale_recorded_conftest_falls_back_unchanged(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, candidate = trees
    conftest = "tests/conftest.py"
    for tree in (root, candidate):
        (tree / conftest).write_text("import os\n", encoding="utf-8")
    payload = _map_payload(root)
    payload["digests"][conftest] = tis.content_digest(b"# recorded before the edit\n")
    _write_map(root, payload)
    reason = f"impact_map_stale_dependency:{conftest}"
    _assert_structural(monkeypatch, _select(root, candidate), reason, root, candidate)
