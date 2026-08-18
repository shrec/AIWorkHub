"""NF-2026-00299: a subprocess spawned by a test must import the tree under test.

The dangerous failure mode: a child spawned inside a worktree has no worktree
path on ``sys.path``, so it imports ``aiworkhub`` through the editable install
(the canonical tree) and the assertion is made against the wrong code -- a
correct candidate fails, and (silently) a broken candidate passes.

These regressions pin both directions using two independent ``aiworkhub``
copies: a "worktree" copy and a "canonical" copy standing in for the editable
install. With the conftest env the child imports the worktree copy; without it,
the child resolves the canonical copy OUTSIDE the worktree and the regression's
assertion fails -- which is exactly the property under test.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent
if str(_TESTS) not in sys.path:
    sys.path.insert(0, str(_TESTS))

from conftest import (  # noqa: E402  (test-support helpers live in conftest)
    child_aiworkhub_file,
    is_beneath,
    make_aiworkhub_tree,
    worktree_pythonpath_env,
)


def _env_without_pythonpath() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}


def test_child_imports_aiworkhub_from_worktree_not_canonical(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    make_aiworkhub_tree(worktree, "worktree-copy")
    canonical = tmp_path / "canonical"
    make_aiworkhub_tree(canonical, "canonical-copy")

    # The conftest env prepends the worktree src ahead of the canonical copy,
    # so a spawned child imports the tree under test even though a canonical
    # copy is also importable.
    env = worktree_pythonpath_env(worktree, extra_src=[canonical / "src"])
    child_file = child_aiworkhub_file(env)

    assert child_file is not None, "child could not import aiworkhub"
    # The child's aiworkhub.__file__ is beneath the worktree root.
    assert is_beneath(child_file, worktree)
    assert not is_beneath(child_file, canonical)


def test_regression_fails_when_child_resolves_aiworkhub_outside_worktree(
    tmp_path: Path,
) -> None:
    worktree = tmp_path / "worktree"
    make_aiworkhub_tree(worktree, "worktree-copy")
    canonical = tmp_path / "canonical"
    make_aiworkhub_tree(canonical, "canonical-copy")

    # WITHOUT the worktree prepend: only the canonical copy is discoverable,
    # reproducing the NF-2026-00299 failure mode where a child resolves
    # aiworkhub through the canonical/editable install rather than the tree
    # under test.
    env = worktree_pythonpath_env(canonical, base_env=_env_without_pythonpath())
    child_file = child_aiworkhub_file(env)

    assert child_file is not None, "child could not import canonical aiworkhub"
    # Demonstrated: the child resolved aiworkhub OUTSIDE the worktree root.
    assert not is_beneath(child_file, worktree)
    assert is_beneath(child_file, canonical)

    # And the regression assertion has teeth: asserting the child imports the
    # worktree fails for this env, so a child that resolves the package outside
    # the worktree can never pass silently.
    with pytest.raises(AssertionError):
        assert is_beneath(child_file, worktree), (
            f"child imported aiworkhub from {child_file}, outside {worktree}"
        )
