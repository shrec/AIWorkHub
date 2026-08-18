"""Shared test helpers (NF-2026-00299).

A test that spawns ``sys.executable`` inside a worker's worktree gets a child
with no worktree path on ``sys.path``: the child imports ``aiworkhub`` through
the editable install -- the CANONICAL tree -- so the assertion is made against
code the candidate was supposed to change. That breaks the evidence chain in
both directions (a correct candidate can fail, and a broken candidate can pass
when the canonical tree happens to satisfy the assertion, silently).

These helpers make a spawned child import the tree under test by putting the
worktree's ``src`` ahead of everything else on ``PYTHONPATH``, plus a probe that
reports which ``aiworkhub`` a child actually loaded and an assertion that it is
beneath the worktree root.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# A minimal child that imports aiworkhub and reports the file it resolved to,
# so the caller can prove WHICH tree the child loaded.
_CHILD_PROBE = "import aiworkhub, sys; sys.stdout.write(getattr(aiworkhub, '__file__', '') or '')"


def is_beneath(path: os.PathLike[str] | str, root: os.PathLike[str] | str) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
    except ValueError:
        return False
    return True


def worktree_pythonpath_env(
    worktree_root: os.PathLike[str] | str,
    *,
    base_env: dict[str, str] | None = None,
    extra_src: list[os.PathLike[str] | str] | None = None,
) -> dict[str, str]:
    """Env whose ``PYTHONPATH`` makes a spawned child import the ``aiworkhub``
    tree checked out under ``worktree_root`` (its ``src`` dir), ahead of any
    editable/canonical install already importable in the parent interpreter.
    """
    src = Path(worktree_root).resolve() / "src"
    env = dict(os.environ if base_env is None else base_env)
    components = [str(src)]
    for extra in extra_src or ():
        components.append(str(Path(extra).resolve()))
    existing = env.get("PYTHONPATH", "")
    if existing:
        components.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(components)
    return env


def child_aiworkhub_file(
    env: dict[str, str], *, code: str = _CHILD_PROBE
) -> Path | None:
    """Run a fresh interpreter that imports ``aiworkhub`` under ``env`` and
    return the resolved path the CHILD actually loaded, or ``None`` if it could
    not import it. Not run under ``-I``/``-E`` on purpose: those ignore
    ``PYTHONPATH`` and would defeat the point of the helper.
    """
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    printed = (result.stdout or "").strip()
    if result.returncode != 0 or not printed:
        return None
    return Path(printed).resolve()


def assert_child_imports_worktree(
    env: dict[str, str], worktree_root: os.PathLike[str] | str
) -> Path:
    """Assert a spawned child resolves ``aiworkhub`` to a file beneath
    ``worktree_root``. Returns the child's ``aiworkhub.__file__``.

    This is the regression's teeth: it fails if the child resolves the package
    outside the worktree (the NF-2026-00299 failure mode).
    """
    child_file = child_aiworkhub_file(env)
    assert child_file is not None, "child could not import aiworkhub at all"
    assert is_beneath(child_file, worktree_root), (
        f"child imported aiworkhub from {child_file}, which is NOT beneath the "
        f"worktree root {Path(worktree_root).resolve()}"
    )
    return child_file


def make_aiworkhub_tree(root: os.PathLike[str] | str, sentinel: str) -> Path:
    """Create ``<root>/src/aiworkhub/__init__.py`` carrying a sentinel, standing
    in for one checked-out ``aiworkhub`` tree (a worktree copy or a canonical
    install copy). Returns the package ``__init__.py`` path.
    """
    package = Path(root) / "src" / "aiworkhub"
    package.mkdir(parents=True, exist_ok=True)
    init = package / "__init__.py"
    init.write_text(f"WORKTREE_SENTINEL = {sentinel!r}\n", encoding="utf-8")
    return init


@pytest.fixture
def worktree_import_env():
    return worktree_pythonpath_env


@pytest.fixture
def spawn_child_aiworkhub_file():
    return child_aiworkhub_file


@pytest.fixture
def assert_imports_from_worktree():
    return assert_child_imports_worktree


@pytest.fixture
def make_aiworkhub_worktree():
    return make_aiworkhub_tree
