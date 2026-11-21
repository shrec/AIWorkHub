"""NF-2026-02298: accept runs the neighbour tests of a changed non-Python path.

A candidate that changed only ``vscode-extension/extension.js`` ran no neighbour test at
accept although a Python test reads that file. Every changed non-test path outside
``src/**.py`` now goes to the impact selector (``reads:<path>``); when the impact lane
cannot decide, the structural lane selects the collected tests whose text names the
repo-relative path (or a distinctive basename). Documentation, ``src/**.py`` modules and
test-only changes keep their earlier behaviour.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from aiworkhub import accept_neighbour_tests as neighbours
from aiworkhub import test_impact_select as tis

EXT = "vscode-extension/extension.js"
MOD = "src/pkg/mod.py"
TEST_X = "tests/test_x.py"
TEST_Y = "tests/test_y.py"
TEST_M = "tests/test_m.py"
TEST_BARE = "tests/test_bare.py"
NODE_X = f"{TEST_X}::test_x"
NODE_M = f"{TEST_M}::test_m"
_FILES = {
    ".gitignore": ".aiworkhub/\n__pycache__/\n",
    EXT: "module.exports = 1;\n",
    "scripts/release_cut.py": "VALUE = 1\n",
    "src/pkg/__init__.py": "",
    MOD: "def f():\n    return 1\n",
    TEST_X: f"def test_x():\n    assert open('{EXT}').read()\n",
    TEST_Y: "def test_y():\n    assert True\n",
    TEST_M: "from pkg import mod\n\n\ndef test_m():\n    assert mod.f() == 1\n",
    TEST_BARE: "def test_b():\n    assert 'extension.js'\n",
}


def _git(repo: Path, *args: str) -> str:
    argv = [
        "git", "-c", "user.name=impact", "-c", "user.email=impact@example.invalid",
        "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args,
    ]
    return subprocess.run(argv, cwd=repo, check=True, capture_output=True, text=True).stdout


def _map_payload(root: Path, *, with_extension: bool) -> dict[str, Any]:
    objects = [f"{MOD}::f", NODE_X, f"{TEST_Y}::test_y", NODE_M, f"{TEST_BARE}::test_b"]
    rows = {
        NODE_X: (TEST_X, [1], [EXT] if with_extension else []),
        f"{TEST_Y}::test_y": (TEST_Y, [2], []),
        NODE_M: (TEST_M, [0, 3], []),
        f"{TEST_BARE}::test_b": (TEST_BARE, [4], []),
    }
    recorded = [MOD, TEST_X, TEST_Y, TEST_M, TEST_BARE] + ([EXT] if with_extension else [])
    return {
        "schema_id": tis.SCHEMA_ID,
        "objects": objects,
        "tests": {
            nodeid: {"file": file, "code": code, "reads": reads, "children": [], "incomplete": False}
            for nodeid, (file, code, reads) in rows.items()
        },
        "digests": {path: tis.content_digest((root / path).read_bytes()) for path in recorded},
        "import_time": [],
        "import_reads": {},
    }


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


def _write_map(root: Path, payload: Any) -> None:
    target = tis.map_directory(root) / tis.MAP_FILE_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload), encoding="utf-8")


def _trees(tmp_path: Path, *, with_extension: bool) -> tuple[Path, Path]:
    root = _make_tree(tmp_path / "canonical", repository=False)
    _write_map(root, _map_payload(root, with_extension=with_extension))
    candidate = _make_tree(tmp_path / "candidate", repository=True)
    (candidate / EXT).write_bytes(b"module.exports = 2;\n")
    (candidate / MOD).write_bytes(b"def f():\n    return int('1')\n")
    (candidate / TEST_Y).write_bytes(b"def test_y():\n    assert 1 == 1\n")
    return root, candidate


def _select(root: Path, candidate: Path | None, changed: Any) -> Any:
    return neighbours.select_neighbour_tests(root, list(changed), [], candidate_root=candidate)


@pytest.fixture(autouse=True)
def _no_testmap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The structural testmap lane relates no test to any module here."""
    monkeypatch.setattr(
        neighbours._advice, "_freshness_states", lambda _root, paths: {p: "fresh" for p in paths}
    )
    monkeypatch.setattr(
        neighbours._advice, "_related_tests", lambda *_a, **_k: ({}, [], False, True)
    )


# (a) the impact lane selects the reading test


def test_a_changed_extension_selects_the_test_that_reads_it(tmp_path: Path) -> None:
    root, candidate = _trees(tmp_path, with_extension=True)
    selection = _select(root, candidate, [EXT])
    assert selection.lane == neighbours.LANE_IMPACT_MAP
    assert selection.lane_reason == "impact_map_fresh:1"
    assert selection.tests == (NODE_X,)
    assert selection.production_paths == (EXT,)
    evidence = selection.evidence()
    assert evidence["lane"] == "impact_map" and evidence["production_paths"] == [EXT]
    assert "reason" not in evidence


def test_a_changed_non_src_python_path_is_a_selector_path(tmp_path: Path) -> None:
    root, _candidate = _trees(tmp_path, with_extension=True)
    assert neighbours._non_python_paths(["scripts/release_cut.py", MOD, TEST_X, "README.md"]) == [
        "scripts/release_cut.py"
    ]
    assert neighbours._non_python_paths(["tests/conftest.py", "docs/a.rst", ".git/x"]) == []
    assert root.is_dir()


# (b) the map lacks the path: the structural lane selects by reference


def test_an_unmapped_extension_selects_the_tests_that_name_it(tmp_path: Path) -> None:
    root, candidate = _trees(tmp_path, with_extension=False)
    selection = _select(root, candidate, [EXT])
    assert selection.lane == neighbours.LANE_STRUCTURAL
    assert selection.lane_reason == f"impact_map_unmapped:{EXT}"
    assert selection.reason == "no_changed_production_python"
    # The full path ranks before a bare, distinctive basename.
    assert selection.tests == (TEST_X, TEST_BARE)
    assert selection.referencing == (TEST_X, TEST_BARE)
    assert selection.referencing_omitted == 0
    assert selection.production_paths == (EXT,)
    assert selection.evidence()["referencing"] == [TEST_X, TEST_BARE]


def test_referencing_is_capped_and_counts_the_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, candidate = _trees(tmp_path, with_extension=False)
    monkeypatch.setattr(neighbours, "MAX_REFERENCING_TESTS", 1)
    selection = _select(root, candidate, [EXT])
    assert selection.referencing == (TEST_X,) and selection.referencing_omitted == 1


def test_a_generic_basename_matches_only_by_its_path(tmp_path: Path) -> None:
    root, candidate = _trees(tmp_path, with_extension=False)
    (root / "tests/test_generic.py").write_text(
        "def test_g():\n    assert 'index.js' and 'web/index.js'\n", encoding="utf-8"
    )
    (root / "tests/test_other_index.py").write_text(
        "def test_i():\n    assert 'index.js'\n", encoding="utf-8"
    )
    selection = _select(root, candidate, ["web/index.js"])
    assert selection.tests == ("tests/test_generic.py",)


# (c) a src/**.py change selects exactly what it selected before


def test_a_src_module_change_is_unchanged(tmp_path: Path) -> None:
    root, candidate = _trees(tmp_path, with_extension=True)
    selection = _select(root, candidate, [MOD])
    assert selection.lane == neighbours.LANE_IMPACT_MAP
    assert selection.tests == (NODE_M,)
    assert selection.production_paths == (MOD,)
    assert selection.referencing == ()
    # The structural lane too: only the stem rule applies, never the non-Python rule.
    structural = neighbours._with_referencing(
        root, neighbours._empty("x", 12, production_paths=(MOD,)), [MOD], []
    )
    assert structural.tests == () and structural.production_paths == (MOD,)


def test_a_src_module_change_names_no_non_python_reference(tmp_path: Path) -> None:
    root, candidate = _trees(tmp_path, with_extension=False)
    (root / "tests/test_loads_mod.py").write_text(
        "def test_l():\n    assert 'mod.py' and 'extension.js'\n", encoding="utf-8"
    )
    selection = _select(root, candidate, [MOD])
    assert selection.referencing == ("tests/test_loads_mod.py",)
    assert selection.production_paths == (MOD,)


# (d) tests-only and documentation-only changes keep today's behaviour


@pytest.mark.parametrize("changed", [[TEST_Y], ["README.md"], ["docs/guide.md"], []])
def test_tests_only_and_docs_only_changes_select_nothing(tmp_path: Path, changed: list[str]) -> None:
    root, candidate = _trees(tmp_path, with_extension=True)
    selection = _select(root, candidate, changed)
    assert selection.tests == ()
    assert selection.reason == "no_changed_production_python"
    assert selection.lane == neighbours.LANE_STRUCTURAL
    assert selection.lane_reason == "impact_map_skipped:no_changed_production_python"
    assert selection.production_paths == () and selection.referencing == ()
