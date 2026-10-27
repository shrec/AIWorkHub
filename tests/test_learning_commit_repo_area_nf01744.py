"""NF-01744: a change spanning several top-level directories still has an area.

_repo_area returned "" when the production paths shared no directory prefix,
and resolve_short_form then refused the inline learning commit at accept with
learning_commit_repo_area_not_derivable. The fallback is the top-level
directory holding the most production paths, ties broken lexicographically.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from aiworkhub.learning_commit import _repo_area, commit_owed  # noqa: E402


def test_the_majority_top_level_directory_is_the_area():
    assert _repo_area(["scripts/a.py", "src/pkg/b.py", "src/pkg/c.py"]) == "src"


@pytest.mark.parametrize(
    "paths",
    [
        ["src/aiworkhub/a.py", "scripts/b.py"],
        ["scripts/b.py", "src/aiworkhub/a.py"],
        ["tools/x.py", "scripts/b.py", "src/a/a.py", "tools/y/z.py", "scripts/c/d.py"],
    ],
)
def test_a_tie_resolves_to_the_lexicographically_first_directory(paths):
    assert _repo_area(paths) == "scripts"


def test_tests_do_not_outvote_the_production_paths():
    paths = ["scripts/a.py", "src/b/c.py", "tests/test_a.py", "tests/test_b.py"]
    assert _repo_area(paths) == "scripts"


def test_only_test_paths_across_top_levels_still_derive():
    assert _repo_area(["tests/test_a.py", "tests/x/test_b.py", "other/tests/test_c.py"]) == "tests"


def test_a_root_level_file_does_not_empty_the_area():
    assert _repo_area(["README.md", "src/aiworkhub/a.py"]) == "src"


@pytest.mark.parametrize(
    ("paths", "area"),
    [
        (["src/aiworkhub/source_graph.py", "tests/test_x.py"], "src/aiworkhub"),
        (["src/aiworkhub/dash/a.py", "src/aiworkhub/dash/b.py"], "src/aiworkhub/dash"),
        (["src/aiworkhub/dash/a.py", "src/aiworkhub/b.py"], "src/aiworkhub"),
        (["tests/test_a.py", "tests/test_b.py"], "tests"),
        (["README.md"], "."),
        ([], ""),
    ],
)
def test_single_prefix_derivations_are_unchanged(paths, area):
    assert _repo_area(paths) == area


def test_commit_owed_over_multi_top_level_paths_names_an_area():
    """The short form reads repo_area from commit_owed; it must not be blank."""
    owed = commit_owed(
        task_id="T", request_id="r" * 32, outcome="accepted",
        changed_paths=["scripts/a.py", "src/pkg/b.py", "src/pkg/c.py", "tests/test_b.py"],
    )
    assert owed["repo_area"] == "src"
    assert owed["repo_area"].strip()
